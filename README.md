# Prospector

**Prospector** is a pipeline + web UI for triaging a large open-PR backlog on a GitHub repository you help maintain: it clusters PRs by the problem they solve, proposes each PR's fate (merge / ask-the-author-to-fix / close), adversarially security-reviews the merge candidates, verifies fixes in a sandbox, and — once a human approves — executes the decisions upstream as a GitHub App.

See `docs/deployments/paperclip.md` for the deployment it was built in: a ~3,000-PR backlog worked down from this app.

## Quick start

Prerequisites: macOS or Linux; `git`; [`uv`](https://docs.astral.sh/uv/) (it fetches the pinned Python itself); the [`gh` CLI](https://cli.github.com/) authenticated to an account that can read the target repo; Node ≥ 24 for the web UI (pnpm is resolved automatically). Docker is needed only if you run the VERIFY phase.

```bash
# 1. clone this repo, then from its root:
./setup.sh                      # uv-locked Python env + frontend deps (idempotent)

# 2. run the app and open the printed frontend URL
uv run prospector serve --dev   # or: ./run-prospector.sh
```

On a checkout with no deployment configured, the app opens a setup wizard
instead of the triage tabs. It takes either route: paste the bundle a teammate
copies from their 🛠️ Setup tab under "Share this deployment" — which carries
the repository, the store, and the repository profile, so one paste is enough —
or answer a few questions to point Prospector at a repository of your own,
where each decision offers an option that works immediately and one that costs
a few minutes. Setup then proceeds one opt-in step at a time: see the
repository, then let a bot write to it, then optionally run automated tasks on
this machine. `/.env.example` documents every option for editing the file
directly; you do not need it to get started.

For a single-process setup without the dev servers, build the frontend once (`pnpm --dir prospector_app/frontend build`, or `npx -y pnpm@11 --dir prospector_app/frontend build` without a pnpm install) and run `uv run prospector serve`.

To make a machine process work rather than just serve the UI — running
verification sandboxes and autofix — run `./setup-worker-machine.sh` on it and
watch the app's 🛠️ Setup tab go green. Any number of machines can; each holds
its own sandbox base, and the queue claim is a compare-and-swap so two never
pick up the same PR.

`uv run prospector --help` lists every subcommand. The Clusters board in the web UI is the front door; `CLAUDE.md` (trust model and operating rules) and `ARCHITECTURE.md` (the data layer) are the two documents to read before going deeper. `STATUS.md` is a generated text snapshot of the store — regenerate it with `uv run prospector status`.

## How it works

The **store** (SQL, via `TRIAGE_STORE_URL` or a local SQLite default) is the single source of truth: one validated row per PR and per cluster, every fact stamped with the `head_sha` it was computed against (so it goes stale automatically when an author pushes). The app serves reads from an incrementally refreshed in-memory snapshot; generated status and briefing Markdown is output only, never parsed back.

Seven idempotent phases plus a deterministic threat scan feed the store:

```
INGEST ─► THREAT SCAN ─► CLUSTER ─► ANALYZE ─► GATE ─► SECURITY ─► VERIFY ─► RESOLVE
```

- **INGEST:** fetch open non-draft PRs and issue links.
- **THREAT SCAN:** apply deterministic attack signatures and the actor blocklist.
- **CLUSTER / ANALYZE:** summarize diffs, group related PRs, and propose dispositions.
- **GATE / SECURITY / VERIFY:** apply quality gates, adversarial review, and
  secretless red→green verification.
- **RESOLVE:** a human approves; the executor performs controlled upstream actions.

Deterministic drivers own selection, validation, and store writes. Agentic
judgment runs through schema-validated batch Workflows or locked-down per-PR
agents; agents never write the store directly. See `pipeline/workflows/README.md`.

### Vocabulary

- **Disposition** (per PR): `merge`, `request-changes` (ask the author for specific fixes, then merge), `close-dup`, `close-fixed`, `close-stale`, `needs-human`.
- **Cluster state**: `needs-analysis`, `awaiting-authors`, `needs-first-party-work` (the feature is wanted but no contributed PR is cleanly salvageable — write our own), `blocked-on-decision`, `security-pending`, `ready`, `done`.

The automatic merge recommendation is strict: **the configured review-provider
bar + passing CI + mergeability + current GREEN security + an author-shipped
verified fix**. Human-initiated merges use `gates.merge_eligibility`: they must
pass every check that actually ran, but missing or inconclusive SECURITY/VERIFY
evidence is not itself a block. The provider and score threshold are deployment
configuration (`TRIAGE_REVIEW_PROVIDER`, `TRIAGE_REVIEW_THRESHOLD`); `none`
disables the external-review requirement.

## Structure

| Folder | Purpose |
|--------|---------|
| `pipeline/` | The store (`store/`), the phase drivers, `gates.py` / `freshness.py` / `taxonomy.py` / `profile.py`, the Workflow scripts, the `prospector` CLI (`cli.py`), and `views.py` (generates `STATUS.md`). |
| `prospector_app/` | The web app: `backend/` (FastAPI), `frontend/` (React/Vite), and `agent/` (Ask-pane helpers and operating context). |
| `issue_triage/` | The **issue** pipeline, on the same substrate as `pipeline/`: its own validated store (`store/`), `issue_freshness.py` / `issue_gates.py` / `issue_model.py` over the shared `pipeline/storekit.py`, and phase drivers (INGEST → CLUSTER → ANALYZE). Imports `pipeline/taxonomy.py`; the app Issues tab projects its store. |
| `alert_triage/` | The **security-alert** pipeline, on the same substrate: GitHub code-scanning / Dependabot / secret-scanning alerts for `TRIAGE_REPO`, read and actioned as the bot App. `alert_store.py` / `alert_model.py` / `alert_freshness.py` / `alert_gates.py` over the shared `pipeline/storekit.py`, plus `alert_ingest.py` (fetch + deterministic PR linking) and `alert_fixed_driver.py` / `find_fixed.py` (the tiered already-fixed pass). Plus `advisory_store.py` / `advisory_model.py` / `advisory_ingest.py` / `advisory_find_fixed.py` for repository security advisories (read-only; no upstream write path) and `security_sweep.py`, the one Control-tab `security-sweep` job over both. The app 🛡️ Alerts tab projects both stores, opening on its Advisories sub-view; secret values are never stored. |

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
   - **Code scanning alerts**: read & write (🛡️ Alerts tab — optional; ingest + dismissal)
   - **Dependabot alerts**: read & write (🛡️ Alerts tab — optional)
   - **Secret scanning alerts**: read & write (🛡️ Alerts tab — optional)
   - **Repository security advisories**: read (🛡️ Alerts → Advisories — optional)

   The alert and advisory permissions are optional: without them the 🛡️ Alerts
   tab reports each source unavailable and everything else works unchanged.
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

Full **CLUSTER**, **ANALYZE**, and **SECURITY** waves interleave deterministic
drivers with the Workflow scripts in `pipeline/workflows/`. **VERIFY** instead
runs per PR through the app queue and `verify_pr.py` worker. See
`pipeline/workflows/README.md` for the current sequences.

## Deployment boundary

This is a **local, single-operator tool**. The backend has no authentication and binds localhost only; the frontend talks to it over the local port. Running it on a shared host or exposing the ports to a network is unsupported — anyone who can reach the backend port can read the store and, on a keyed machine, execute upstream writes.

The security boundary is the machine, not the app:

- **Reads** run as your local `gh` login.
- **Writes** run as the GitHub App, and only on a machine holding the app's private key (`TRIAGE_BOT_KEY_FILE`). No readable key ⇒ the executor mints no token and every write is forced to dry-run.
- Executor writes use the configured GitHub App, refuse an empty bot token, and
  are appended to the activity log; merges additionally pass the per-PR gate in
  `pipeline/gates.py`. Confirmed bot writes from the optional chat agent use its
  own explicit command allowlist; issue closes route through the executor and are
  recorded in that activity log, while its other bot writes are not. Resubmit
  pushes and branch updates run as the operator and append best-effort activity
  entries; configured feedback-repository issue filing also runs as the operator
  and is not recorded there.

## Platform contract & versioning

Python is pinned to `==3.14.*` (`.python-version` + `uv.lock`); that exact pin is the tested contract, and `uv` downloads it on any supported platform — you never install Python by hand. The frontend needs Node ≥ 24 and pnpm ≥ 11 (`setup.sh` resolves pnpm via `npx` if it isn't installed). Supported platforms: macOS and Linux.

Versioning is `0.x`, bumped manually in `pyproject.toml` at meaningful milestones; there is no release cadence.

## Safety

`CLAUDE.md` is authoritative. In short: reads run as the local login. The app
executor writes as the configured app, logs every attempt, and applies the
per-PR gate to merges. The optional chat agent has a separate, confirmation-based
allowlist for non-merge writes; its issue closes use the executor and activity
log, while its other bot-authenticated writes do not. Operator-identity resubmits
append best-effort entries. With no readable `TRIAGE_BOT_KEY_FILE`, neither path
can write as the app, though local helpers and configured feedback-repository
issue filing remain available. Don't hand-run `gh pr merge/close/comment` against
the configured upstream; use the app's controlled paths.
