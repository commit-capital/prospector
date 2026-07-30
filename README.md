# Prospector

**Prospector** is a pipeline + web UI for triaging a large open-PR backlog on a GitHub repository you help maintain: it clusters PRs by the problem they solve, proposes each PR's fate (merge / ask-the-author-to-fix / close), adversarially security-reviews the merge candidates, verifies fixes in a sandbox, and — once a human approves — executes the decisions upstream as a GitHub App.

See `docs/deployments/paperclip.md` for the deployment it was built in: a ~3,000-PR backlog worked down from this app.

## Quick start

Prerequisites: macOS or Linux; `git`; [`uv`](https://docs.astral.sh/uv/) (it fetches the pinned Python itself); the [`gh` CLI](https://cli.github.com/) authenticated to an account that can read the target repo; Node ≥ 24 for the web UI (pnpm is resolved automatically). Docker is needed only if you run the VERIFY phase.

```bash
# 1. clone this repo, then from its root:
./setup.sh                      # uv-locked Python env + frontend deps (idempotent)

# 2. configure the target repository
cp .env.example .env            # then set TRIAGE_REPO=owner/name (and see Configuration below)

# 3. pull the open PRs into the store (read-only; local SQLite by default)
uv run prospector ingest

# 4. run the app and open the printed frontend URL
uv run prospector serve --dev   # or: ./run-prospector.sh
```

For a single-process setup without the dev servers, build the frontend once (`pnpm --dir prospector_app/frontend build`, or `npx -y pnpm@11 --dir prospector_app/frontend build` without a pnpm install) and run `uv run prospector serve`.

`uv run prospector --help` lists every subcommand. The Clusters board in the web UI is the front door; `CLAUDE.md` (trust model and operating rules) and `ARCHITECTURE.md` (the data layer) are the two documents to read before going deeper. `STATUS.md` is a generated text snapshot of the store — regenerate it with `uv run prospector status`.

## How it works

The **store** (SQL, via `TRIAGE_STORE_URL` or a local SQLite default) is the single source of truth: one validated row per PR and per cluster, every fact stamped with the `head_sha` it was computed against (so it goes stale automatically when an author pushes). The app serves reads from an in-memory snapshot it keeps in sync with the store incrementally; all markdown is generated *from* the store, never parsed back.

Seven idempotent phases feed the store:

```
INGEST ─► CLUSTER ─► ANALYZE ─► GATE ─► SECURITY ─► VERIFY ─► RESOLVE
  │         │          │          │        │          │          │
  │         │          │          │        │          │          └─ app: human approves,
  │         │          │          │        │          │             executor acts upstream as
  │         │          │          │        │          │             the bot (gated merges)
  │         │          │          │        │          └─ run the PR's test in a secretless
  │         │          │          │        │             sandbox against a pinned main: red
  │         │          │          │        │             before the fix, green after; adds
  │         │          │          │        │             verified-fix to the merge bar
  │         │          │          │        └─ 3-lens adversarial review + refuting
  │         │          │          │           verifier, on GATE-clean merge candidates
  │         │          │          │           only; RED → needs-human + reopen cluster
  │         │          │          └─ which merge candidates are clean enough to review
  │         │          │             (review bar ∧ CI ∧ mergeable ∧ fresh)
  │         │          └─ per-cluster dispositions + outcome; a below-bar merge pick
  │         │             reads as request-changes with author asks (derived on read)
  │         └─ diff-grounded summaries → semantic clusters (stable IDs)
  └─ open non-draft PRs + issue links → store
```

Each phase has a deterministic **driver** (`*_driver.py` — wave selection, validation, store writes) and, where it needs judgment, an agentic **Workflow** script (`pipeline/workflows/*.js`) with schema-validated structured output. Drivers never trust an agent to write the store directly. See `pipeline/workflows/README.md` for the run commands.

### Vocabulary

- **Disposition** (per PR): `merge`, `request-changes` (ask the author for specific fixes, then merge), `close-dup`, `close-fixed`, `close-stale`, `needs-human`.
- **Cluster state**: `needs-analysis`, `awaiting-authors`, `needs-first-party-work` (the feature is wanted but no contributed PR is cleanly salvageable — write our own), `blocked-on-decision`, `security-pending`, `ready`, `done`.

The merge bar is strict: **the configured review provider's bar + CI passing + mergeable + security GREEN (or a logged override) + verified-fix**. The review provider is deployment config (`TRIAGE_REVIEW_PROVIDER`; `none` requires no external review). Anything short of the bar routes to request-changes or close — the app never suggests merging a flagged PR.

## Structure

| Folder | Purpose |
|--------|---------|
| `pipeline/` | The store (`store/`), the phase drivers, `gates.py` / `freshness.py` / `taxonomy.py` / `profile.py`, the Workflow scripts, the `prospector` CLI (`cli.py`), and `views.py` (generates `STATUS.md`). |
| `app/` | The web app. `backend/` (FastAPI over the store) + `frontend/` (React/Vite). The human triage + execution surface. |
| `issue_triage/` | The **issue** pipeline, on the same substrate as `pipeline/`: its own validated store (`store/`), `issue_freshness.py` / `issue_gates.py` / `issue_model.py` over the shared `pipeline/storekit.py`, and phase drivers (INGEST → CLUSTER → ANALYZE). Imports `pipeline/taxonomy.py`; the app Issues tab projects its store. |

## Configuration

The system reads a single gitignored `/.env` at the repo root. Copy `/.env.example` to `/.env` and fill in as needed; `pipeline/settings.py`, `setup.sh`, and Vite all read it. Real shell environment variables override anything in the file.

**Repository profile.** Repository-specific policy vocabulary — the subsystem taxonomy the CLUSTER phase and issue triage classify against, the path→risk-tier map, the CODEOWNERS gated paths/owners, trusted/automation authors, dependency manifests, test/artifact path conventions, review-harness PR-template policy, and VERIFY full-suite adapter — lives in a JSON **repository profile** selected by `TRIAGE_PROFILE` (see `profile.example.json` for the shape; `pipeline/profile.py` validates it strictly and fails loudly on any malformed or unknown field). Match terms are regular expressions searched against the lowercased title/body — write them in lowercase. The `test_paths` patterns are also compiled by the app frontend as JavaScript RegExp — keep them in the shared regex subset (no `(?P<…>)` named groups, inline flags, or possessive quantifiers; `\Z` differs between engines). Without a profile the generic default applies: no subsystem vocabulary, so every PR and issue classifies as `other` — clustering still works, just without subsystem grouping, risk ranking knows only the shared supply-chain surface, no path is CODEOWNERS-gated, no author is trusted, dependency/test/artifact conventions fall back to cross-ecosystem defaults, the review harness enforces no PR-template sections, and the baseline/regress leg is skipped until `verify.suite` is configured. The `dependency_manifests` list also drives the VERIFY sandbox's dependency-refusal gate — narrowing it below the generic default weakens that protection. The real profile lives beside `.env` as the gitignored `profile.json` at the repo root.

**Backing store.** With `TRIAGE_STORE_URL` unset, each store component uses a local SQLite file under its own directory — fine for dev, CI, or solo work. Set `TRIAGE_STORE_URL` to a shared PostgreSQL database URL to point the whole system (and every operator machine) at one shared store. SQLite and PostgreSQL are the supported store dialects.

No example, demo, or seed store ships with the project. A fresh checkout starts
empty, and `prospector ingest` populates it from the repository you configure.

The SQL store can be exported to a tree of JSON files at any time — a backup / inspection escape hatch (the JSON is never read back as a store; re-importing it is the reverse `import` subcommand):

```bash
# PR store
uv run python pipeline/store_migrate.py dump @env <output-dir>

# Issue store
uv run python issue_triage/issue_store_migrate.py dump @env <output-dir>
```

**Bot identity (live writes).** Upstream writes execute as a GitHub App, and each deployment registers its own — the app's private key is its identity, so one app cannot be shared across deployments. Without one, everything still works read-only: the executor mints no token and every write runs dry.

1. [Register a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app) on the account or org that owns `TRIAGE_REPO`. Name it what you want writes attributed as; no webhook needed. Grant these repository permissions (the set the write paths need):
   - **Contents**: read & write (squash-merges)
   - **Issues**: read & write (issue close/comment/reopen)
   - **Pull requests**: read & write (PR comment/close/review/merge)
   - **Metadata**: read (implied)
2. Keep the app install-restricted (**Only on this account**) and install it on the `TRIAGE_REPO` owner, granting the triaged repo. `pipeline/get-bot-token.sh` selects the installation whose account is the owner of `TRIAGE_REPO`, so a stray installation elsewhere is never used — but there's no reason to allow one.
3. Generate a private key in the app settings and save the PEM outside the repo (e.g. `~/.config/<app>/private-key.pem`), then wire `.env`:
   - `TRIAGE_BOT_APP_ID` — the app's numeric id (on the app's settings page)
   - `TRIAGE_BOT_LOGIN` — the app's slug (writes are attributed to `<slug>[bot]`)
   - `TRIAGE_BOT_KEY_FILE` — path to the PEM; the PEM itself stays outside the repo and environment

Only machines that should execute live writes get the key; the app id and login are not secrets. Verify the wiring with `bash pipeline/get-bot-token.sh` (needs `node` and `jq`) — it prints a one-hour installation token on success and a specific error naming the missing piece otherwise.

## Operations — who runs what

Two kinds of operations: ones **you** run (a single self-contained command, or an app button), and ones **the agent** runs (an agent orchestrating a multi-stage phase). The dividing line is fan-out: a job that targets one PR or one cluster, or that's pure deterministic bookkeeping, is a command you run; a job that summarizes/clusters/analyzes the whole corpus interleaves an agentic `workflows/*.js` step and is agent-driven.

### Operations you run

All from the repo root. The first three are deterministic and cheap; the last three spawn a single headless agent (they spend metered tokens, but are still one command you invoke). Most are also buttons on the app **Control** tab.

```bash
# refresh open PRs + issue links into the store (read-only gh; cheap)
uv run prospector ingest            # [--max N]

# deterministic threat scan over cached diffs (no agents, no metered tokens)
uv run prospector threat-scan       # [--only 123,456]

# regenerate STATUS.md from the store
uv run prospector status

# one cluster: refresh member facts from GitHub + re-classify dispositions/outcome
uv run prospector triage-cluster --cluster <cid>

# one cluster: re-summarize + re-cluster its members (split a mis-clustered PR out)
uv run prospector recluster --cluster <cid>

# one PR: 3-lens adversarial security review (also the ↻ Run button in the app)
uv run prospector security-review --pr <n>
```

Triage itself is an app operation: open the Clusters board, approve a plan, and the executor acts upstream as the configured bot (gated, logged). You never hand-run `gh pr merge/close/comment` against the triaged repo.

### Operations the agent runs

The full **CLUSTER**, **ANALYZE**, **SECURITY**, and **VERIFY** phases run the whole wave: each interleaves a driver's deterministic halves (`cluster_driver.py` / `analyze_driver.py` / `security_driver.py` / `verify_driver.py` subcommands — `wave`, `fetch-diffs`, `groups`, `commit-summaries-dir`, `commit-clusters-dir`, `eligible`, `commit`, `prepare-base`, …) with an agentic Workflow script (`pipeline/workflows/{summarize,cluster,analyze,security,verify_blind,verify_judge}.js`). You kick these off by asking the agent (or via the triage skills), not by typing one command — and the `workflows/*.js` scripts are never run by hand. See `pipeline/workflows/README.md` for the exact sequence.

## Deployment boundary

This is a **local, single-operator tool**. The backend has no authentication and binds localhost only; the frontend talks to it over the local port. Running it on a shared host or exposing the ports to a network is unsupported — anyone who can reach the backend port can read the store and, on a keyed machine, execute upstream writes.

The security boundary is the machine, not the app:

- **Reads** run as your local `gh` login.
- **Writes** run as the GitHub App, and only on a machine holding the app's private key (`TRIAGE_BOT_KEY_FILE`). No readable key ⇒ the executor mints no token and every write is forced to dry-run.
- Executor writes use the configured GitHub App, refuse an empty bot token, and
  are appended to the activity log; merges additionally pass the per-PR gate in
  `pipeline/gates.py`. Confirmed bot writes from the optional chat agent use its
  own explicit command allowlist and are not recorded in that activity log.
  Resubmit pushes and branch updates run as the operator and append best-effort
  activity entries; configured feedback-repository issue filing also runs as the
  operator and is not recorded there.

## Platform contract & versioning

Python is pinned to `==3.14.*` (`.python-version` + `uv.lock`); that exact pin is the tested contract, and `uv` downloads it on any supported platform — you never install Python by hand. The frontend needs Node ≥ 24 and pnpm ≥ 11 (`setup.sh` resolves pnpm via `npx` if it isn't installed). Supported platforms: macOS and Linux.

Versioning is `0.x`, bumped manually in `pyproject.toml` at meaningful milestones; there is no release cadence.

## Safety

`CLAUDE.md` is authoritative. In short: reads run as the local login. The app
executor writes as the configured app, logs every attempt, and applies the
per-PR gate to merges. The optional chat agent has a separate, confirmation-based
allowlist for non-merge writes; bot-authenticated chat writes are not part of the
executor's activity log, while operator-identity resubmits append best-effort
entries. With no readable `TRIAGE_BOT_KEY_FILE`, neither path can write as the
app, though local helpers and configured feedback-repository issue filing remain
available. Don't hand-run `gh pr merge/close/comment` against the configured
upstream; use the app's controlled paths.
