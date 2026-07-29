# Prospector — Claude operating rules

This repo triages the open PRs and issues on the repository configured by
`TRIAGE_REPO`, decides which PRs to **merge, ask-the-author-to-fix, or close**,
and executes approved decisions upstream. The cockpit web app
(`review_cockpit/`) is the human surface; the pipeline (`pipeline/`) produces
the data it shows. See `README.md` for the full workflow, and `ARCHITECTURE.md`
for how state flows (SQL store → cockpit snapshot → live overlay).

## Trust model (read this first)

`TRIAGE_REPO` and `TRIAGE_BOT_LOGIN` are required deployment configuration.
They live in the gitignored root `.env` (or the process environment), so a
checkout has no implicit repository or bot identity. The cockpit executes
approved triage actions directly on `TRIAGE_REPO` as the configured GitHub App.
The project `PreToolUse` hook in `.claude/settings.json` reads the same
configuration and denies direct GitHub write commands targeting `TRIAGE_REPO`;
with no target configured, those commands fail closed.

- **Reads** run as the operator's default local `gh` login. Do not set
  `GH_CONFIG_DIR`.
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
  The cockpit calls `gh pr merge --squash` against `TRIAGE_REPO` through the
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
- **The cockpit chat agent** (`review_cockpit/backend/chat.py`) has a separate,
  curated upstream-write path. With a bot token and conversational
  confirmation it may run PR edit/comment/close/reopen/review, issue
  create/close/reopen/comment/edit, and workflow reruns against `TRIAGE_REPO`
  as `TRIAGE_BOT_LOGIN`. Without a token those upstream writes are withheld;
  they never fall back to the operator's login. A separate helper may always
  file an issue only on `COCKPIT_FEEDBACK_REPO` as the operator. The agent's
  resubmit helper also uses the operator identity for contributor-fork pushes,
  but is available only on a machine that can mint the bot token. These paths
  do not use `safety_guard`'s `BOT_WRITE_ALLOW` or the per-PR merge gate. The
  chat agent cannot merge.

Every executor write, including a dry-run, is appended to the cockpit activity
log. Resubmit pushes and branch updates append best-effort entries under the
operator's identity. Bot-authenticated chat writes and feedback issue filing are
not recorded in that log. Executor enforcement lives in
`review_cockpit/backend/safety_guard.py`: an allowlist that permits only
comment/close/reopen/review as the configured bot plus the dedicated
`bot_merge_run` path, and refuses any write with an empty token.

## The pipeline (`pipeline/`)

One canonical store, seven phases plus a deterministic threat-scan backstop. **The store is the only source of truth** — all markdown (`STATUS.md`, briefings) is generated output, never parsed back.

- **Store** (`store.py` over `storekit.py`): a SQL database — one row per PR (its sections `meta / signals / drift / summary / cluster / analysis / security / issues / threat / greptile_review / verify` carried in a JSON `data` column, alongside mirror columns and a `saved_at` write-stamp), one row per cluster, a `runs` ledger table, and singleton `registries` rows (the durable threat registry, action items). The backend is `TRIAGE_STORE_URL` (a shared SQL database) or a local SQLite default under `pipeline/store/`. **Validated on write; `storekit`/`store.py` is the ONLY accessor** — never hand-write rows.
- **Freshness** (`freshness.py`): every fact section is stamped `against_head_sha`. When a PR head moves, its analysis/security/signals go stale **automatically** — `is_current()` is the single check. No manual invalidation.
- **Gates** (`gates.py`): the ONE policy module. `pr_clean` (not-malicious ∧ the configured review provider's bar ∧ CI passing ∧ mergeable ∧ fresh), `security_eligible`, `verify_eligible`, `merge_allowed` (pipeline auto-recommend — requires a current `verified-fix` alongside GREEN security), `merge_eligibility` (human-initiated cockpit merge — drops the disposition requirement, treats never-run security or never-run verification as OK, and blocks on verification only when one ran and did not confirm the fix), the derived `cluster_state` (computed on read, never stored), and `merge_demotion` — the ONE merge-pick consequence (security verdict + verify outcome + quality-gate bar), derived at read time by `Pr.disposition`/`rationale`/`asks` over a stored-verbatim ANALYZE verdict, so nothing derived is ever stored and a cleared fact heals the read in place. **The configured review provider's bar is a hard merge requirement** — the provider and threshold are deployment config (`pipeline/review_policy.py`, selected by `TRIAGE_REVIEW_PROVIDER`). A merge pick below that configured threshold reads as `request-changes`, with an ask to address the review feedback and reach the bar. A deployment with `TRIAGE_REVIEW_PROVIDER=none` requires no external review. A `threat` verdict of `malicious` is a sticky hard block (fails closed, no staleness exemption). A PR may belong to several clusters (straddlers, #196); each cluster proposes a disposition for its members and `reconcile_disposition` picks the PR's single disposition by severity precedence (`needs-human > close-dup > close-fixed > close-stale > request-changes > merge` — most-blocking wins).
- **Threats** (`threats.py`): the ONE threat-detection policy — attack-pattern signatures (obfuscated self-decoders, capability smuggles, build-config require-injection, EOL-churn camouflage) scanned over a PR's diff, plus the durable actor blocklist + incident log in the store's `threats` registry. `threat_scan.py` is the deterministic driver (Phase 0.5). Greptile/CI are quality signals, **not** a security verdict — this is the supply-chain backstop.
- **Profile** (`profile.py`): the ONE repository-policy profile — repository-specific vocabulary as validated JSON data (`TRIAGE_PROFILE` path; strict parse, hard error on unknown/malformed fields). Owns the subsystem taxonomy, the path→risk-tier glob map, the CODEOWNERS gating policy (gated globs + owners), trusted/automation authors, dependency manifests, the test/artifact path rules, and the review-harness PR-template policy (`review-new-pr/harness` reads the same JSON standalone via stdlib `json`, never importing `pipeline`).
- **Taxonomy** (`taxonomy.py`): the ONE subsystem-classification accessor; the vocabulary itself is repository policy in the active profile (`profile.py`, selected by `TRIAGE_PROFILE`, generic default = everything `other`); `issue_triage` imports it.

Phases (each idempotent; drivers own the deterministic half, Workflow scripts the agentic half):
1. **INGEST** (`ingest.py`) — fetch open non-draft PRs + issue links into the store. Cheap, re-runnable.
1.5. **THREAT SCAN** (`threat_scan.py` + `threats.py`) — scan cached diffs for attack signatures + check authors against the blocklist; stamp `threat`, block actors, log incidents. Deterministic, no agents. A `malicious` flag removes the PR from the gate permanently.
2. **CLUSTER** (`cluster_driver.py` + `workflows/summarize.js`) — diff-grounded summaries → semantic clusters with stable IDs. A PR's `cluster` backref is a list (`cluster.ids`); a straddler can belong to more than one cluster, and each cluster retains its per-member proposed dispositions in `cluster.proposals`.
3. **ANALYZE** (`analyze_driver.py` + the analyze workflow) — per-cluster dispositions + outcome, stored verbatim; a merge pick's blockers (sub-5/5 review, a security verdict, a verify outcome) derive its effective disposition at read time (`gates.merge_demotion` via `Pr.disposition`), so a re-run or signal refresh that clears the blocker heals the read with nothing re-stored.
4. **GATE** (`gates.py`) — which merge candidates are clean enough for security review.
5. **SECURITY** (`security_driver.py` + `workflows/security.js`) — 3-lens adversarial review + refuting verifier on gated merge candidates only; RED flips the PR to needs-human and reopens every cluster it belongs to.
6. **VERIFY** (`verify_driver.py` + `workflows/verify_blind.js` + `workflows/verify_judge.js`) — run the PR's test in a secretless Docker sandbox against a pinned `main`: red before the fix, green after, each phase its own container so the host reads the verdict from its exit code. Dynamic verification never runs against a credentialed deployment. A green that exits failing is still accepted when its parsed failing set is contamination the red run already carried — every green failure also failed red, none named by the PR's own test hunks (`gates.green_accepted`); such a verified-fix carries a `dirty-green` finding and reads as partial evidence at merge time (`verify_signals_incomplete`), so it never auto-recommends. A blind adequacy verdict commits to the store before any run; `gates.verify_outcome` computes the outcome from that verdict, the exit codes, and the post-run judgment, so a blind-unfaithful-yet-clean-red→green escalates to needs-human. A PR that ships no test gets an AUTHOR pass: an agent authors a new test file (validated fail-closed — new files under test paths only; the driver builds the patch and derives the command), and a clean confirmed red→green with a matching red reason records `agent-verified` — corroborating evidence that satisfies `merge_eligibility` but never `merge_allowed`, whose bar stays an author-shipped `verified-fix`. The profile may configure merge-gate *lanes* (`verify.compile_cmd`, `verify.build_cmd`) — whole-repo commands run over the PR-patched tree after a clean confirm; `verified-fix`/`agent-verified` require every configured lane green (a failed lane reads `regressed`, an infra-broken lane `escalate`, and a record missing configured lanes is incomplete evidence named at merge time). The worker refreshes the base pin daily when master has moved, so lanes measure a ≤24h-old base.
7. **RESOLVE** — human approves in the cockpit; the executor acts upstream as `TRIAGE_BOT_LOGIN`.

See `pipeline/workflows/README.md` for the exact run commands. Run phases from the CLI or the cockpit Control tab; `views.py` regenerates `STATUS.md` from the store.

## Vocabulary

Per-PR **disposition**: `merge | request-changes | close-dup | close-fixed | close-stale | needs-human`. A PR has exactly one disposition even when it belongs to several clusters — it is reconciled from each cluster's proposal by severity precedence (most-blocking wins), and `analysis.from_cluster` records which cluster's proposal won.
Cluster **outcome / state**: `merge-ready | awaiting-authors | needs-first-party-work | close-out | blocked-on-decision`, plus derived `security-pending`, `ready`, `done`, `needs-analysis`. ("Synthesize" → `needs-first-party-work`; "Stack" is gone — complementary PRs are just coordinated merges noted in rationale.)

## Conventions

- **Comments and docstrings describe the code as it is now.** The test: every comment must be true of the code *as it stands alone* — no comparison to any other version, whether past, future, or hypothetical. If a clause only makes sense by contrast, cut it. This rules out not just temporal references ("previously…", "this used to…", "now does X", forward-references to unbuilt features) but also **counterfactual rationale** — "would otherwise…", "instead of X", "rather than Y", "doing X here would be blocked". Just say what the code does, plus the bare present reason if it's non-obvious ("synchronous so the popup blocker allows it"), never the contrast against a rejected alternative. If the code no longer does something, its comment goes too. The git history is the record of change; the source is the record of the present.
- **Type every function signature as precisely as the value allows.** Annotate every parameter and the return — and use the *most specific* type that's true: `list[Cluster]`, not `list`; `dict[int, Pr]`, not `dict`; `str | None`, not bare `str` when it can be None. When you change what a function returns or accepts, update its annotation in the same edit — never leave a stale type, and never weaken a precise type to a bare `list`/`dict`/untyped just to silence it (that's a regression). `uv run pyright pipeline issue_triage review_cockpit/backend review-new-pr/harness` (the command in `.github/workflows/pipeline-tests.yml`) must stay at **0 errors** — it's a CI gate; run it when you touch the domain model. The gate checks each tree as a whole directory, not a hand-maintained file list, so every new source module is covered automatically; `test_*.py` is excluded via `pyrightconfig.json` since tests deliberately do `None`-unsafe things.
- **No quoted / string type annotations.** Every module has `from __future__ import annotations`, so all annotations are already lazy strings at runtime — never write `-> "model.Cluster"` or `x: "Pr"`. Write them bare (`-> model.Cluster`, `x: Pr`). For a type that can't be imported at runtime (the `store ↔ model` cycle: `pipeline/store.py` ↔ `pipeline/model.py`, or `gates`/`freshness`/cockpit modules referencing `model`), make the name resolvable to the checker with an `if TYPE_CHECKING:` import (`if TYPE_CHECKING: from pipeline import model` / `from pipeline.model import Pr, Cluster`) — the `__future__` import means the annotation is never evaluated at runtime, so there is no cycle and the quotes are unnecessary.
- **Imports are qualified — `from pipeline import …`, `from review_cockpit.backend import …`, `from issue_triage import …`.** The source roots are installed (editable) packages, each at its natural path under the repo root: `pipeline`, `issue_triage`, and `review_cockpit` (whose backend is `review_cockpit.backend`), registered in `pyproject.toml` `[tool.setuptools]`. A new module is automatically a submodule of its package — import it qualified from anywhere. **Never add a `sys.path.insert`, and never import a sibling by bare name** (`import store`); that's the one mechanism the whole tree shares, so it resolves identically at runtime, under pytest, and from the CLI. The only exceptions are the standalone tools that run under a bare `python3` outside the install — `review-new-pr/harness/` and `review_cockpit/agent/*` — which bootstrap their own package roots at the top of the entry script.
- **Name tools for what they do, standalone** — not for the workflow that happens to invoke them. A tool that processes one cluster or reviews one PR is `triage_cluster` / `security_review`, not `rerun_*` (it may be the first run, not a re-run).
- **Reads are fine anywhere; writes to `TRIAGE_REPO` go through the cockpit's sanctioned paths only** so they are gated, bot-identified, and logged. Do not hand-run `gh pr merge/close/comment/review` against the triaged repository.
- The store is SQL (`TRIAGE_STORE_URL` — a shared SQL database — or a local SQLite default under `pipeline/store/`), not committed files. `pipeline/cache/` (diffs, raw gh) is gitignored. Bulk or destructive record edits go through `pipeline/store_edit.py` (dry-run default, automatic pre-image snapshot, runs-ledger entry) — never ad-hoc scripts against `TRIAGE_STORE_URL`.
- **`schema.STORE_SCHEMA_VERSION` guards stale writers.** Any PR that changes store record shape in a way older code mishandles must bump it. The store stamps the version on first write; a checkout whose constant is behind the store's stamp can read but not write (`storekit.assert_writable`, escape hatch `TRIAGE_STORE_ALLOW_STALE=1`).
- **Dev-env config lives in three parallel files — keep them in sync.** `.conductor/settings.toml` (Conductor), `.superset/config.json` (Superset), and `.claude/launch.json` (Claude Code desktop run configurations) all wire up the same dev entry points — `setup.sh` and `uv run pr-triager serve --dev` (launch.json runs the backend and the Vite dev server as two separate configurations, resolving the frontend toolchain via `frontend-toolchain.sh`). A change to any one's setup/run commands must be mirrored in the others.
- **Keep `pyproject.toml` dependency lists sorted** — `[project].dependencies` and every `[dependency-groups]` list stay alphabetical (case-insensitive). When you add a dep, insert it in order rather than appending, so the lists never drift.
- Tests: `uv run pytest` from the repo root runs all three suites (`pipeline/tests`, `issue_triage/tests`, and `review_cockpit/backend/tests`). The environment is uv-locked to Python 3.14.6 (`.python-version` + `uv.lock`); `uv run <cmd>` auto-syncs it — no manual venv activation. `source ./activate` is optional convenience.
- **Ruff is a CI gate (`uv run ruff check .`) and the tree is clean — keep it clean** (unlike the frontend's baseline, the bar here is **zero** findings). Config is in `pyproject.toml` `[tool.ruff]`: pyflakes (F), pycodestyle (E/W), pyupgrade (UP), and `N999` — the invalid-module-name rule that blocks re-introducing a non-importable package directory like the old `review-cockpit/`. Several UP rules enforce conventions pyright can't (no quoted annotations, `X | None` over `Optional`). `uv run ruff check --fix` auto-fixes most; naming rules beyond N999 are intentionally off.
- **The TS frontend (`review_cockpit/frontend/`) has its own gate, separate from pyright/pytest — run it after any frontend change.** From `review_cockpit/frontend/`: `pnpm run build` (`tsc -b && vite build`) — the `tsc` step is the type gate and must pass with **0 errors** — and `pnpm run lint`. ESLint carries a repo-wide baseline of pre-existing errors, so the bar is **add no new lint errors** (lint just your files with `pnpm exec eslint <files>`), not a clean repo. Use **pnpm**, never npm (install marker `.modules.yaml`).
- **Type the TypeScript as precisely as the Python.** The signature-typing rule above applies to the frontend too: annotate params and returns, parameterize generics (`useState<Pr | null>`, not bare), and prefer `unknown` + narrowing over `any`. Match each file's surrounding style — the cockpit uses inline prop types and double-quoted strings; don't refactor those to named prop interfaces.
