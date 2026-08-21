# Prospector — Claude operating rules

This repo triages the open PRs and issues on the repository configured by
`TRIAGE_REPO`, decides which PRs to **merge, ask-the-author-to-fix, or close**,
and executes approved decisions upstream. The web app
(`prospector_app/`) is the human surface; the pipeline (`pipeline/`) produces
the data it shows. See `README.md` for the full workflow, and `ARCHITECTURE.md`
for how state flows (SQL store → app snapshot → live overlay).

## Trust model (read this first)

`TRIAGE_REPO` and `TRIAGE_BOT_LOGIN` are required deployment configuration.
They live in the gitignored root `.env` (or the process environment), so a
checkout has no implicit repository or bot identity. The app executes
approved triage actions directly on `TRIAGE_REPO` as the configured GitHub App.
The project `PreToolUse` hook in `.claude/settings.json` reads the same
configuration and denies direct GitHub write commands targeting `TRIAGE_REPO`.
A `gh` write names its own repository, so with no target configured every one
of them fails closed. A `git push` is judged by the destination it resolves to
— the named remote, else the branch's push remote, else `origin` — and an
unresolvable destination fails closed; with no target configured a push is held
to the checkout it runs in, which is how a fresh clone with no `.env` pushes its
own branches while still being unable to reach the triage repository.

- **Reads** run as the operator's default local `gh` login. Do not set
  `GH_CONFIG_DIR`.
- **Pushes to contributor PR head branches** use one shared `resubmit` flow with
  caller-selected identity. Interactive chat resubmits run as the confirming
  operator after dropping the injected App token. The unattended autofix worker
  explicitly selects the machine user in `TRIAGE_PUSH_LOGIN`, a GitHub *user*
  authenticating by its pinned SSH key alone (`TRIAGE_PUSH_SSH_KEY_FILE`), with
  no API token. Pushing to a fork head branch rides "Allow edits from
  maintainers", which grants push to maintainer *users*, so an App installation
  token can never do it. `assert_push_target` refuses any ref that is not the open
  PR's own head — a repository ruleset targets branches and grants bypass to
  actors, so it cannot scope a restriction to one account, and this is where the
  user's reach is actually bounded. With the worker identity unset every
  unattended push refuses; it never falls back to the operator or the App.
- **Writes** use a token minted from `TRIAGE_BOT_KEY_FILE` by
  `pipeline/get-bot-token.sh`, and are attributed to `TRIAGE_BOT_LOGIN`. When a
  bot token cannot be minted, the executor obtains no token and **every write is
  forced to dry-run**. `executor.live_possible()` probes token minting and is the
  single source of truth used by `identities()` and `caps.py`. Dry-run is a
  per-machine fallback, not deployment policy: a machine that can mint the bot
  token executes approved writes for real. The hard write-gate is unchanged:
  `bot_run` and `bot_merge_run` refuse every write with an empty token, and the
  bot subprocess environment never falls back to the operator's login.
- **Merges are upstream squash-merges as the configured bot, gated per PR.**
  The app calls `gh pr merge --squash` against `TRIAGE_REPO` through the
  bot's installation token; it does not cherry-pick into a fork under a human
  identity. The executor gates a human-initiated merge on
  `pipeline/gates.py:merge_eligibility`: gate-clean ∧ security
  GREEN-or-never-run ∧ not CODEOWNERS-gated. ANALYZE disposition is not
  required, so a clean Easy-Lane PR the pipeline never analyzed is mergeable.
  `merge_allowed` (disposition=merge ∧ gate-clean ∧ current GREEN security ∧
  current verified-fix) is the stricter gate the pipeline uses to
  auto-recommend merge. A live merge also passes the deterministic compile
  preflight (`pipeline/compile_preflight.py` +
  `gates.compile_preflight_gate`), which runs the profile's
  `verify.compile_cmd` over the current default-branch HEAD plus the PR diff in
  the sandbox. Anything but a clean pass blocks, including missing or broken
  sandbox infrastructure. The preflight runs only on a live merge; dry-run
  previews never boot a container. `gh pr edit` is never allowed on the
  executor path.
- **The app chat agent** (`prospector_app/backend/chat.py`) has a separate,
  curated upstream-write path. When token minting is available, conversational
  confirmation unlocks PR edit/comment/close/reopen/review, issue
  create/close/reopen/comment/edit, and workflow reruns against `TRIAGE_REPO`
  as `TRIAGE_BOT_LOGIN`. Without a token those upstream writes are withheld;
  they never fall back to the operator's login. A separate helper may always
  file an issue only on `PROSPECTOR_FEEDBACK_REPO` as the operator. The agent's
  resubmit helper uses the confirming operator's identity for interactive
  contributor-branch pushes and is advertised when the session can mint the bot
  token. The worker opts into its configured machine identity separately. These
  paths do not use the per-PR merge gate. Chat PR close, reopen, and review
  operations, plus issue closes, call their corresponding executor paths; other
  upstream chat writes use `prospector_app/agent/gh-write`, which validates the
  operation, pins the configured repository, and mints a token for each invocation.
  Chat reads use the operator environment. The chat agent cannot merge.

Every executor write, including a dry-run, is appended to the app activity
log. Resubmit pushes and branch updates append best-effort entries under the
identity selected by their caller. Chat PR close, reopen, and review operations,
plus issue closes, are executor writes and appear in the log; other
bot-authenticated chat writes and feedback issue filing do not. Executor
enforcement lives in
`prospector_app/backend/safety_guard.py`: an allowlist that permits only
comment/close/reopen/review as the configured bot plus the dedicated
`bot_merge_run` path, and refuses any write with an empty token.

## The pipeline (`pipeline/`)

One canonical store, seven phases plus a deterministic threat-scan backstop. **The store is the only source of truth** — all markdown (`STATUS.md`, briefings) is generated output, never parsed back.

- **Store** (`store.py` over `storekit.py`): a SQL database — one row per PR (its sections `meta / signals / drift / summary / cluster / analysis / security / issues / threat / greptile_review / verify / verify_request / fix_request` carried in a JSON `data` column, alongside mirror columns and a `saved_at` write-stamp), one row per cluster, a `runs` ledger table, and singleton `registries` rows (the durable threat registry, action items). The backend is `TRIAGE_STORE_URL` (a shared SQL database) or a local SQLite default under `pipeline/store/`. **Validated on write; `storekit`/`store.py` is the ONLY accessor** — never hand-write rows.
- **Freshness** (`freshness.py`): every fact section is stamped `against_head_sha`. When a PR head moves, its analysis/security/signals go stale **automatically** — `is_current()` is the single check. No manual invalidation.
- **Gates** (`gates.py`): the ONE policy module. `pr_clean` (not-malicious ∧ the configured review provider's bar ∧ CI passing ∧ mergeable ∧ fresh), `security_eligible`, `verify_eligible`, `merge_allowed` (pipeline auto-recommend — requires a current `verified-fix` alongside GREEN security), `merge_eligibility` (human-initiated app merge — drops the disposition requirement; treats never-run, stale, pending, and unverifiable verification as non-blocking; requires no reason for those merges; blocks actual negative verification evidence; and permits an explicit `escalate` only with a logged reason), the derived `cluster_state` (computed on read, never stored), and `merge_demotion` — the ONE merge-pick consequence (security verdict + verify outcome + quality-gate bar), derived at read time by `Pr.disposition`/`rationale`/`asks` over a stored-verbatim ANALYZE verdict, so nothing derived is ever stored and a cleared fact heals the read in place. **The configured review provider's bar is a hard merge requirement** — the provider and threshold are deployment config (`pipeline/review_policy.py`, selected by `TRIAGE_REVIEW_PROVIDER`). A merge pick below that configured threshold reads as `request-changes`, with an ask to address the review feedback and reach the bar. A deployment with `TRIAGE_REVIEW_PROVIDER=none` requires no external review. A `threat` verdict of `malicious` is a sticky hard block (fails closed, no staleness exemption). A PR may belong to several clusters (straddlers, #196); each cluster proposes a disposition for its members and `reconcile_disposition` picks the PR's single disposition by severity precedence (`needs-human > close-dup > close-fixed > close-stale > request-changes > merge` — most-blocking wins).
- **Threats** (`threats.py`): the ONE threat-detection policy — attack-pattern signatures (obfuscated self-decoders, capability smuggles, build-config require-injection, EOL-churn camouflage) scanned over a PR's diff, plus the durable actor blocklist + incident log in the store's `threats` registry. `threat_scan.py` is the deterministic driver (Phase 0.5). Greptile/CI are quality signals, **not** a security verdict — this is the supply-chain backstop.
- **Profile** (`profile.py`): the ONE repository-policy profile — repository-specific vocabulary as validated JSON data (`TRIAGE_PROFILE` path; strict parse, hard error on unknown/malformed fields). Owns the subsystem taxonomy, the path→risk-tier glob map, the CODEOWNERS gating policy (gated globs + owners), trusted/automation authors, dependency manifests, the test/artifact path rules, and the review-harness PR-template policy (`review-new-pr/harness` reads the same JSON standalone via stdlib `json`, never importing `pipeline`).
- **Taxonomy** (`taxonomy.py`): the ONE subsystem-classification accessor; the vocabulary itself is repository policy in the active profile (`profile.py`, selected by `TRIAGE_PROFILE`, generic default = everything `other`); `issue_triage` imports it.

Phases (each idempotent; drivers own the deterministic half, Workflow scripts the agentic half):
- **0 INGEST** (`ingest.py`) — fetch open non-draft PRs + issue links into the store. Cheap, re-runnable.
- **0.5 THREAT SCAN** (`threat_scan.py` + `threats.py`) — scan cached diffs for attack signatures + check authors against the blocklist; stamp `threat`, block actors, log incidents. Deterministic, no agents. A `malicious` flag removes the PR from the gate permanently.
- **1 CLUSTER** (`cluster_driver.py` + `workflows/summarize.js`) — diff-grounded summaries → semantic clusters with stable IDs. Identical heads get identical memberships, and open PRs with direct (`explicit` / lower-confidence `body-ref`) links to the same issue are deterministically given a common cluster even when semantic summaries diverge. A PR's `cluster` backref is a list (`cluster.ids`); a straddler can belong to more than one cluster, and each cluster retains its per-member proposed dispositions in `cluster.proposals`.
- **2 ANALYZE** (`analyze_driver.py` + the analyze workflow) — per-cluster dispositions + outcome, stored verbatim; a merge pick's blockers (below-bar review, a security verdict, a verify outcome) derive its effective disposition at read time (`gates.merge_demotion` via `Pr.disposition`), so a re-run or signal refresh that clears the blocker heals the read with nothing re-stored.
- **3 GATE** (`gates.py`) — which merge candidates are clean enough for security review.
- **4 SECURITY** (`security_driver.py` + `workflows/security.js`) — 3-lens adversarial review + refuting verifier on gated merge candidates only; RED flips the PR to needs-human and reopens every cluster it belongs to.
- **6 VERIFY** (`verify_driver.py` + `verify_pr.py`) — run the PR's test in a secretless Docker sandbox against the machine's own pinned default branch: red before the fix, green after, each phase in its own container. Dynamic verification never runs against a credentialed deployment. A green that exits failing is accepted only when its parsed failing set is contamination the red run already carried and none is named by the PR's test hunks (`gates.green_accepted`); that partial evidence never auto-recommends merge. A blind adequacy verdict commits before any run, and `gates.verify_outcome` computes the outcome from agent judgments plus host-observed exits. For a PR without tests, an agent may author new test files through a fail-closed validator; a confirmed red→green records `agent-verified`, which supports human `merge_eligibility` but never automatic `merge_allowed`. Configured compile/build lanes run over the patched tree; failures become `regressed`, infrastructure errors become `escalate`, and missing lane evidence is surfaced as incomplete. Any number of machines run this phase: the queue claim is a compare-and-swap, each machine holds its own base pin (`verify_base` is keyed by hostname) and refreshes it daily after the default branch moves, and every result records the base it was proven against. The idle hunter claims a PR's security review the same way, so two machines never both spend one.
- **7 RESOLVE** — human approves in the app; the executor acts upstream as `TRIAGE_BOT_LOGIN`.

**AUTOFIX** (`prospector_app/backend/fix_queue.py` + `fix_worker.py`) sits beside
the phases rather than in them: a per-PR `fix_request` section any app queues and
the worker on `TRIAGE_FIX_WORKER=1` drains, pushing to the contributor's head
branch as the machine user. Actions are `update` (merge the base in), `rebase`
(replay onto current base behind a pinned lease), and `fix` (an agent authors a
change). A `fix` runs two agents inside a `resubmit prepare` clone:
`pipeline/author_fix.py` writes the change against a goal — the operator's own
typed guidance, else the profile's fixable gates read off the current review
findings and failing checks — and `pipeline/review_fix.py` then tries to refute
the finished patch from a fresh context with read-only tools. Only an explicit
`safe` passes; a malformed, timed-out, or crashed reviewer reads as unsafe. In
between, the patch is held to the files the agent reported
(`author_fix.assert_disclosed`) and re-gated on the paths it really touched, so
an agent cannot author its way onto a withheld path. A mechanical `rebase` that pauses on real
conflicts escalates — for operator-clicked requests only, never the hunter's —
to a fourth action, `resolve`: a locked-down agent resolves the conflicted
paths inside a merge of current base into the head (`resubmit prepare --merge`
+ `pipeline/resolve_conflicts.py`), the result passes the compile preflight,
and it parks as `awaiting-review` with a per-file rationale, keeping its
worktree — the only action that does, and so the only one whose approval its
own machine must push. `gates.fix_eligibility` holds `resolve` to the CODEOWNERS
and deny-glob bar over the conflicted paths, with no profile opt-in; a resolve
never autopushes regardless of `TRIAGE_FIX_AUTOPUSH`, and approval pushes the
kept merge commit with no history rewrite. `gates.fix_eligibility` is the ONE
policy —
fail-closed on a malicious threat verdict, any recorded RED security verdict, a
CODEOWNERS-gated path, a path the profile's `autofix.deny_globs` names, a
non-open PR, and (for an unguided `fix`) a profile naming no
`autofix.fixable_gates`. Operator guidance is its own opt-in: a named human
typing the goal authorizes the fix where the profile does not, and the change
still parks for that same human's approval. Guidance chooses the job only —
every other block answers identically whatever was typed, so it can never widen
the bot's reach. That reach is what these blocks bound, not the merge boundary:
an autofixed PR still faces `merge_eligibility` unchanged. Every action is probed before it is pushed —
the mechanics run in full (`resubmit update --probe` for a base merge, `prepare
--rebase` for a replay), the resulting tree goes through
`compile_preflight.run_for_patch`, and the request parks as `awaiting-review`
with its evidence unless `TRIAGE_FIX_AUTOPUSH` names the action. A parked
request keeps no worktree: `push_approved` rebuilds one. A mechanical request
re-derives against current base and refuses if that no longer resolves, so an
approval means "push this against base as it stands now" rather than replaying
an older verdict; an agent-authored `fix` parks its whole reviewed patch and the
approval clones the head and re-applies those exact bytes (`resubmit apply`),
never a fresh agent attempt. Both can therefore be pushed by any autofix
machine, not only the one that produced them. `TRIAGE_FIX_AUTOHUNT=1` lets an idle worker queue the mechanical actions
itself, gated by `gates.fix_huntable` — the review provider's bar on top of
`fix_eligibility`, and deliberately not `mergeable`, CI, or a GREEN security
verdict, none of which a PR that needs updating can have. Every hunted action
gets one unattended attempt per head SHA — a terminal outcome rests the PR
until the author pushes, and only an operator's re-queue retries the same
head. `TRIAGE_FIX_HUNT_FIX=1`
additionally lets the hunter queue unguided `fix` actions, on the inverse
population — CI passing, mergeable, review score below the bar and scored at
the current head — at most `TRIAGE_FIX_HUNT_LIMIT`
(default 3) in flight, and only where the profile names `autofix.fixable_gates`.
A pushed `fix` re-triggers the review provider as the bot (Activity-logged) and
starts the backend wait that ingests the fresh score. Every action that ends —
refused, failed, parked for review, or pushed — appends a `fix:single` entry to
the runs ledger carrying its action and its one-line reason, which is where an
outcome outlives the `fix_request` the next queue click overwrites; the app's
fix run history reads that lane, and `fix_history_backfill.py` seeds it from the
endings a store already holds. The queue view itself holds an ending for half an
hour, so a run that starts and finishes between two polls is still readable.

**ALERTS** (`alert_triage/`) is a parallel family beside PRs and issues:
GitHub code-scanning / Dependabot / secret-scanning alerts for `TRIAGE_REPO`,
stored in the shared SQL store's `alerts` table (keyed by
`alert_store.alert_id(source, number)`), stamped `against_updated_at`. Reads
AND writes authenticate as the bot App — the App needs the three alert
read/write permissions granted upstream; a missing permission just marks that
source unavailable. `alert_ingest.py` fetches all states and computes
deterministic PR links (`link_prs.py`); `alert_fixed_driver.py` +
`find_fixed.py` run the tiered already-fixed pass (GitHub state → deterministic
manifest/diff/text matching → headless-agent wave) writing `links`/`fix_scan`.
`alert_gates.dismiss_eligibility` is the ONE dismissal policy (fixed-evidence
reasons need a current fixed/likely-fixed verdict; judgment reasons need an
operator note); the executor's `dismiss_alert` runs it through the
`ALERT_WRITE_ALLOW` safety-guard path, Activity-logged, dry-run forced with no
token. Secret values are never fetched or stored — only type + file locations.
GitHub secret scanning (secrets already in the repo) is deliberately separate
from `threats.py` (incoming PR diffs); they are never coupled. The app 🛡️
Alerts tab projects this store.

See `pipeline/workflows/README.md` for the exact run commands. Run phases from the CLI or the app Control tab; `views.py` regenerates `STATUS.md` from the store.

## Vocabulary

Per-PR **disposition**: `merge | request-changes | close-dup | close-fixed | close-stale | needs-human`. A PR has exactly one disposition even when it belongs to several clusters — it is reconciled from each cluster's proposal by severity precedence (most-blocking wins), and `analysis.from_cluster` records which cluster's proposal won.
Cluster **outcome / state**: `merge-ready | awaiting-authors | needs-first-party-work | close-out | blocked-on-decision`, plus derived `security-pending`, `ready`, `done`, `needs-analysis`. ("Synthesize" → `needs-first-party-work`; "Stack" is gone — complementary PRs are just coordinated merges noted in rationale.)

## Conventions

- **Comments and docstrings describe the code as it is now.** The test: every comment must be true of the code *as it stands alone* — no comparison to any other version, whether past, future, or hypothetical. If a clause only makes sense by contrast, cut it. This rules out not just temporal references ("previously…", "this used to…", "now does X", forward-references to unbuilt features) but also **counterfactual rationale** — "would otherwise…", "instead of X", "rather than Y", "doing X here would be blocked". Just say what the code does, plus the bare present reason if it's non-obvious ("synchronous so the popup blocker allows it"), never the contrast against a rejected alternative. If the code no longer does something, its comment goes too. The git history is the record of change; the source is the record of the present.
- **A docstring earns its place only when the body isn't self-evident**, and it is as short as it can be. Match the comment density of the code around it: a one-line accessor sitting beside undocumented siblings takes none, and neither does a test whose name already says what it asserts. Never restate the signature, the function name, or the test name in prose.
- **Type every function signature as precisely as the value allows.** Annotate every parameter and the return — and use the *most specific* type that's true: `list[Cluster]`, not `list`; `dict[int, Pr]`, not `dict`; `str | None`, not bare `str` when it can be None. When you change what a function returns or accepts, update its annotation in the same edit — never leave a stale type, and never weaken a precise type to a bare `list`/`dict`/untyped just to silence it (that's a regression). `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` (the command in `.github/workflows/pipeline-tests.yml`) must stay at **0 errors** — it's a CI gate; run it when you touch the domain model. The gate checks each tree as a whole directory, not a hand-maintained file list, so every new source module is covered automatically; `test_*.py` is excluded via `pyrightconfig.json` since tests deliberately do `None`-unsafe things.
- **No quoted / string type annotations.** Every module has `from __future__ import annotations`, so all annotations are already lazy strings at runtime — never write `-> "model.Cluster"` or `x: "Pr"`. Write them bare (`-> model.Cluster`, `x: Pr`). For a type that can't be imported at runtime (the `store ↔ model` cycle: `pipeline/store.py` ↔ `pipeline/model.py`, or `gates`/`freshness`/app modules referencing `model`), make the name resolvable to the checker with an `if TYPE_CHECKING:` import (`if TYPE_CHECKING: from pipeline import model` / `from pipeline.model import Pr, Cluster`) — the `__future__` import means the annotation is never evaluated at runtime, so there is no cycle and the quotes are unnecessary.
- **Imports are qualified — `from pipeline import …`, `from prospector_app.backend import …`, `from issue_triage import …`.** The installed source packages live at `pipeline/`, `issue_triage/`, `alert_triage/`, and `prospector_app/`, registered in `pyproject.toml` `[tool.setuptools]`. Import them qualified everywhere. **Never add a `sys.path.insert`, and never import a sibling by bare name** (`import store`). The standalone tools under `review-new-pr/harness/` and `prospector_app/agent/*` are the exceptions; their entry scripts bootstrap their package roots for bare `python3` execution.
- **Name tools for what they do, standalone** — not for the workflow that happens to invoke them. A tool that processes one cluster or reviews one PR is `triage_cluster` / `security_review`, not `rerun_*` (it may be the first run, not a re-run).
- **Reads are fine anywhere; writes to `TRIAGE_REPO` go through the app's sanctioned paths only** so they are gated, bot-identified, and logged. Do not hand-run `gh pr merge/close/comment/review` against the triaged repository.
- The store is SQL (`TRIAGE_STORE_URL` — a shared SQL database — or a local SQLite default under `pipeline/store/`), not committed files. `pipeline/cache/` (diffs, raw gh) is gitignored. Bulk or destructive record edits go through `pipeline/store_edit.py` (dry-run default, automatic pre-image snapshot, runs-ledger entry) — never ad-hoc scripts against `TRIAGE_STORE_URL`.
- **`schema.STORE_SCHEMA_VERSION` guards stale writers.** Any PR that changes store record shape in a way older code mishandles must bump it. The store stamps the version on first write; a checkout whose constant is behind the store's stamp can read but not write (`storekit.assert_writable`, escape hatch `TRIAGE_STORE_ALLOW_STALE=1`).
- **Dev-env config lives in three parallel files — keep them in sync.** `.conductor/settings.toml` (Conductor), `.superset/config.json` (Superset), and `.claude/launch.json` (Claude Code desktop run configurations) all wire up the same dev entry points — `setup.sh` and `uv run prospector serve --dev` (launch.json runs the backend and the Vite dev server as two separate configurations, resolving the frontend toolchain via `frontend-toolchain.sh`). A change to any one's setup/run commands must be mirrored in the others.
- **Keep `pyproject.toml` dependency lists sorted** — `[project].dependencies` and every `[dependency-groups]` list stay alphabetical (case-insensitive). When you add a dep, insert it in order rather than appending, so the lists never drift.
- Tests: `uv run pytest` from the repo root runs all four suites (`pipeline/tests`, `issue_triage/tests`, `alert_triage/tests`, and `prospector_app/backend/tests`). The environment is uv-locked to Python 3.14.6 (`.python-version` + `uv.lock`); `uv run <cmd>` auto-syncs it — no manual venv activation. `source ./activate` is optional convenience.
- **Ruff is a CI gate (`uv run ruff check .`) and the tree is clean — keep it clean** (unlike the frontend's baseline, the bar here is **zero** findings). Config is in `pyproject.toml` `[tool.ruff]`: pyflakes (F), pycodestyle (E/W), pyupgrade (UP), and `N999` — the invalid-module-name rule that keeps every package directory importable (a hyphenated name is not). Several UP rules enforce conventions pyright can't (no quoted annotations, `X | None` over `Optional`). `uv run ruff check --fix` auto-fixes most; naming rules beyond N999 are intentionally off.
- **The TS frontend (`prospector_app/frontend/`) has its own gate, separate from pyright/pytest — run it after any frontend change.** From `prospector_app/frontend/`: `pnpm run build` (`tsc -b && vite build`) — the `tsc` step is the type gate and must pass with **0 errors** — and `pnpm run lint`. ESLint carries a repo-wide baseline of pre-existing errors, so the bar is **add no new lint errors** (lint just your files with `pnpm exec eslint <files>`), not a clean repo. Use **pnpm**, never npm (install marker `.modules.yaml`).
- **Type the TypeScript as precisely as the Python.** The signature-typing rule above applies to the frontend too: annotate params and returns, parameterize generics (`useState<Pr | null>`, not bare), and prefer `unknown` + narrowing over `any`. Match each file's surrounding style — the app uses inline prop types and double-quoted strings; don't refactor those to named prop interfaces.
