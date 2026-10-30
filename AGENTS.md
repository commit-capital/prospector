# Prospector — agent operating rules

Prospector triages the open PRs and issues of the repository named by
`TRIAGE_REPO`: it decides which PRs to merge, send back, or close, fixes what it
can, and carries out approved actions upstream. `pipeline/` produces the data,
and the web app (`prospector_app/`) is where a human acts on it.

This file holds the invariants an agent must not break and a map to the code.
The code is the record of behavior: read the module before relying on a detail.
For setup and commands see `README.md` and `docs/`, for how state flows see
`ARCHITECTURE.md`, and for the phase command sequences see
`pipeline/workflows/README.md`.

## Trust model (read this first)

`TRIAGE_REPO` and `TRIAGE_BOT_LOGIN` come from the gitignored root `.env` or the
process environment. A checkout without them has no target and no bot identity.
It boots into the setup wizard, and `app.py` answers every other `/api/*` route
with `409 {"unconfigured": true}`.

- **Reads** run as the operator's local `gh` login. Do not set `GH_CONFIG_DIR`.
- **Writes to `TRIAGE_REPO` go only through `prospector_app/backend/safety_guard.py`**,
  as the GitHub App named by `TRIAGE_BOT_LOGIN`, using a token
  `pipeline/get-bot-token.sh` mints from `TRIAGE_BOT_KEY_FILE`. Each write path
  there (`bot_run`, `bot_merge_run`, `chat_bot_run`, `alert_bot_run`,
  `propose_bot_run`, `label_bot_run`, `close_bot_run`) has its own allowlist. Every path refuses an
  empty token or an empty bot login and never falls back to the operator's login.
  A machine that cannot mint a token does all executor writes as dry-runs
  (`executor.live_possible`). Executor writes, dry-runs included, go to the
  Activity log. Widening any allowlist needs security-focused tests.
- **Issue closes are operator-directed**, except `issue_triage/close_out.py`'s
  through `close_bot_run`, which never closes a maintainer's issue.
- **Unattended abandoned-PR closes** are decided by `gates.abandoned_close`
  alone and go through `executor.execute_pr` as the bot.
- **Never hand-run `gh` writes against the triaged repository.** The
  `PreToolUse` hook (`.claude/hooks/deny-triage-writes.py`) denies `gh pr`/`gh
  issue` writes to `TRIAGE_REPO` and every mutating `gh api` call. It judges a
  `git push` by the remote it resolves to and fails closed. Run `git push` as
  its own command from the repo root.
- **Merges** are `gh pr merge --squash` as the bot, one PR at a time. A
  human-initiated merge must pass `gates.merge_eligibility`, then a fresh threat
  scan at the pinned head (`executor._threat_rescan`), then, live only, the
  compile preflight (`pipeline/compile_preflight.py`). The pipeline's own
  merge recommendation is the stricter `gates.merge_allowed`. The chat agent
  cannot merge.
- **Pushes to contributor head branches** go through `resubmit`, and
  `assert_push_target` refuses any ref that is not the open PR's own head.
  Interactive pushes run as the confirming operator. Unattended pushes run as
  `TRIAGE_PUSH_LOGIN` with its SSH key alone, and refuse when that is unset.
  Commits are authored as the App's bot (`resubmit_identity.commit_identity`).
- **Agents that read outsiders' text are locked down.** A headless agent
  (`pipeline/headless_agent.run_agent`) reads only the roots it is handed, gets
  only the environment variables its caller names, has no `git` rule beyond
  `prospector_app/agent/git-read`, and runs contributor code only in the Docker
  sandbox. The host decides every outcome from what it observed (sandbox
  exits, gates). An agent's word never decides an outcome on its own.
- **The chat agent** (`prospector_app/backend/chat.py`, manual
  `prospector_app/agent/context.md`) writes upstream only through the curated
  helpers after the operator confirms. Its environment is
  `safety_guard.agent_env`, and its Claude read rules deny `.env` and the key
  files (`claude_backend._secret_denies`).
- **`.env` has two writers, each with an allowlist.**
  `worker_control.set_flags` writes only the settings `settings_registry` marks
  editable. `backend/onboarding.py` writes the wizard's keys step by step and
  refuses `connect` and `join` once `settings.configured()` is true, so no HTTP
  call can repoint a running deployment.

## The pipeline

The PR pipeline runs these steps in order. Each step is idempotent and safe to
re-run. A step marked *worker* also runs unattended on a machine with that lane
switched on (Setup tab → worker switches, `TRIAGE_*_WORKER`).

| Step | What it does | Code | Trigger |
|---|---|---|---|
| INGEST | Fetches open PRs and their issue links into the store | `pipeline/ingest.py` | `uv run prospector ingest` · Control `ingest` · *worker* `pr_watch` |
| THREAT SCAN | Applies deterministic attack signatures and the actor blocklist; no agents | `pipeline/threat_scan.py`, `threats.py` | `uv run prospector threat-scan` · Control `threat-scan` · *worker* `threat_refresh` |
| CLUSTER | Summarizes diffs and groups related PRs | `pipeline/cluster_driver.py`, `cluster_pass.py` | workflows README (full pass) · Control `cluster-new` · *worker* `TRIAGE_CLUSTER_WORKER` |
| ANALYZE | Proposes a disposition for each PR in a cluster | `pipeline/analyze_driver.py` | Control `analyze-clusters` · `uv run prospector triage-cluster --cluster <id>` |
| GATE | Decides which candidates are clean enough to review further | `pipeline/gates.py` | evaluated on read |
| SECURITY | Adversarial review of clean PRs → RED / YELLOW / GREEN | `pipeline/security_review.py`, `security_driver.py` | `uv run prospector security-review --pr <n>` · *worker* verify hunter |
| VERIFY | Docker sandbox shows the PR's test failing before the fix and passing after | `pipeline/verify_driver.py`, `verify_pr.py` | PR page / Control `verify-pr` · *worker* `TRIAGE_VERIFY_WORKER` |
| RESOLVE | A human approves; the executor acts upstream as the bot | `prospector_app/backend/executor.py` | the app |

Lanes alongside the pipeline:

- **Design review** (`pipeline/design_review.py`): a Prospector-run reviewer of
  reuse, layering, conventions and test strength. It runs in the verify
  hunter's rotation, or via Control `design-review`.
- **Autofix** (`backend/fix_queue.py`, `fix_worker.py`): update, rebase,
  agent-written fixes, conflict resolves and description rewrites, pushed to
  contributor branches. Eligibility is `gates.fix_eligibility`, and a change
  parks for approval unless `TRIAGE_FIX_AUTOPUSH` names its action.
- **Issues** (`issue_triage/`): issue ingest, analyze and find-fixed (Control
  jobs), and the issue-fix factory, which reproduces, fixes, proves, reviews
  and proposes a PR (`python -m issue_triage.fix_lane` /
  `python -m issue_triage.propose`, *worker* `TRIAGE_ISSUE_FIX_WORKER`).
- **Alerts and advisories** (`alert_triage/`): GitHub code-scanning,
  Dependabot, secret-scanning alerts and repository advisories (Control
  `security-sweep`).

Unattended agent work is metered by `pipeline/capacity.py`. A failing machine
trips its lanes via `pipeline/worker_health.py`. Every phase and lane appends
to the runs ledger in the store.

## Where policy lives

Each decision has one owner. Change the decision there, call it from everywhere
else, and never re-derive it at a call site.

- `pipeline/gates.py`: PR cleanliness, the merge bars, security and
  verify eligibility, autofix eligibility, disposition reconciliation.
- `issue_triage/issue_gates.py`: the issue-fix bars. `alert_triage/alert_gates.py`:
  alert dismissal.
- `pipeline/threats.py`: attack signatures and the blocklist.
- `pipeline/review_policy.py` + `reviewers.py`: which automated reviewers
  gate, and each one's bar.
- `pipeline/freshness.py`: whether a fact is current. Facts are stamped with
  the head SHA (PRs) or update time (issues) they were computed against, and go
  stale on their own.
- `pipeline/profile.py`: repository-specific policy as validated JSON
  (`TRIAGE_PROFILE`): taxonomy, risk tiers, CODEOWNERS gating, trusted authors,
  sandbox commands.
- `pipeline/settings_registry.py`: every environment variable, its default,
  and whether the Setup page may edit it.

## The store

- One SQL database is the only source of truth: `TRIAGE_STORE_URL`, or a local
  SQLite file under `pipeline/store/`. Markdown such as `STATUS.md` is generated
  output and is never parsed back.
- Access PR, cluster, run and registry rows only through `pipeline/store.py` /
  `storekit.py` (issues: `issue_triage/issue_store.py`, alerts:
  `alert_triage/alert_store.py`). Rows are validated on write. Bulk or
  destructive edits go through `pipeline/store_edit.py`. Never use ad-hoc
  scripts.
- A change to record shape that older code would mishandle bumps
  `schema.STORE_SCHEMA_VERSION`. Code behind the store's version can read but
  not write.
- Store facts, not conclusions. Derived values such as a PR's effective
  disposition are computed on read, so a fixed upstream fact corrects the
  reading without a re-store.

## Vocabulary

- **Disposition** (per PR): `merge | request-changes | close-dup | close-fixed |
  close-wontfix | close-stale | needs-human`. A PR in several clusters takes the
  most blocking of their proposals (`gates.reconcile_disposition`).
- **Cluster outcome**: `merge-ready | awaiting-authors | needs-first-party-work |
  close-out | blocked-on-decision`. Derived states add `security-pending`,
  `ready`, `done`, and `needs-analysis`.
- **Security verdict**: `RED | YELLOW | GREEN`.

## Conventions

- **Simplicity first, in code and prose.** Write the shortest code, comment,
  doc or prompt that does the job, and say each thing once. A cleanup leaves
  the file shorter.
- **Comments describe the code as it is now.** No history ("previously", "now
  does") and no contrast with rejected alternatives ("instead of", "would
  otherwise"). Give only the bare present reason when the reason isn't obvious.
  Git history is the record of change.
- **A docstring earns its place only when the body isn't self-evident.** Match
  the comment density of the surrounding code, and never restate a signature or
  test name.
- **Type every signature as precisely as the value allows**, in Python and
  TypeScript alike: `list[Cluster]`, not `list`; `str | None` when it can be
  None; `unknown` plus narrowing, not `any`. Update the annotation in the same
  edit that changes the value. Start every module with
  `from __future__ import annotations` and never quote an annotation. A type
  that would cause an import cycle goes under `if TYPE_CHECKING:`.
- **Imports are qualified** (`from pipeline import store`). Never add
  `sys.path.insert` or import a sibling by bare name. The exceptions are scripts
  run outside the installed environment (`review-new-pr/harness/`,
  `prospector_app/agent/`, the frontend e2e server), which bootstrap their own
  roots.
- **Name tools for what they do**, not for the workflow that calls them
  (`security_review`, not `rerun_security`).
- **Register every environment variable** in `pipeline/settings_registry.py`.
  `test_settings_registry.py` catches literal reads that aren't registered.
  Prefer a sensible default or a Setup-page control to a variable an operator
  has to know about.
- **Keep the dev-env files in sync:** `.conductor/settings.toml`,
  `.superset/config.json`, and `.claude/launch.example.json` all wire up
  `setup.sh` and `uv run prospector serve --dev`. Edit the launch *example*;
  `setup.sh` generates `.claude/launch.json` from it.
- **Keep `pyproject.toml` dependency lists alphabetical.**
- **Use pnpm, never npm**, in `prospector_app/frontend/`.

### Gates (CI enforces the first three)

```bash
uv run pytest                     # all four suites; Python is uv-locked, no venv activation
uv run ruff check .               # zero findings
uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness   # zero errors
cd prospector_app/frontend && pnpm run build && pnpm exec eslint <changed files>   # tsc 0 errors; add no lint errors
```

## Editing this file

Edit this file only when an invariant above changes: a new upstream write path,
identity, gate owner, or pipeline step. Don't add a section for a feature, and
don't restate a constant, default, cadence, threshold, or gate condition. Name
the module that owns it. Feature documentation lives in the module's docstring,
in `docs/`, or in the PR description.
