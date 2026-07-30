# Architecture — the data layer

How a fact gets from GitHub into the store and out to a cockpit request. This is
the part that isn't obvious from any single file: the SQL store, the cockpit's
in-memory snapshot, and the live sweep. For *what the system does* and the
trust model see `README.md` and `CLAUDE.md`; for the phase run-commands see
`pipeline/workflows/README.md`. This doc is the map of *where state lives and how
it flows* — it points at the source files that are the detail-of-record rather
than restating them.

## One picture

```
 GitHub ──INGEST/phases──►  SQL store  ──watermark sync──►  cockpit snapshot  ──►  /api/* reads
   ▲    ▲                  (storekit)     (data.py)          (in-memory dicts)
   │    │ live sweep            │
   │    └ (freshness_live) ─────┤  persists PR closed/merged/mergeable
   │      GraphQL, read-only    │  drift straight into meta.state / signals
   └──────── executor / chat write upstream as configured bot ◄────┘
              (separate controlled write paths; executor actions logged)
```

## 1. The store is SQL

One database is the single source of truth. There is no committed store data and
no JSON-file store — all markdown (`STATUS.md`, briefings) is **generated output,
never parsed back**.

- **Accessor:** `pipeline/store.py` over `pipeline/storekit.py`. This is the
  ONLY accessor — never hand-write rows. `issue_triage/issue_store.py` mirrors it
  for issues over the same `storekit` core.
- **Schema** (`pipeline/schema.py`): a row per PR and per cluster (the fact
  sections `meta / signals / drift / summary / cluster / analysis / security /
  issues / threat` ride in a JSON `data` column alongside mirror
  columns and a `saved_at` write-stamp), a `runs` ledger, singleton `registries`
  rows (durable threat blocklist + incident log, action items, the live-sweep
  timestamp), and the cockpit's own `activity` and `chat_messages` tables. Issues
  add `issues` / `issue_clusters`.
- **Backend selection** (`storekit.resolve_url`): an explicit `--store DIR`
  (tests, CLI) → `sqlite:///DIR/store.db`; else `TRIAGE_STORE_URL` (a shared
  SQL database) verbatim; else a local SQLite default under the store's own
  directory (`pipeline/store/`, `issue_triage/store/`). All three resolve to a
  SQLAlchemy URL — there is no non-SQL path.
- **Supabase note:** use the **transaction pooler (port 6543)**, not the session
  pooler (5432) — see `.env.example`. The engine is configured to match:
  `NullPool` + prepared statements disabled for the pooler, 10s connect timeout
  (`storekit.get_engine`).
- **Freshness** (`pipeline/freshness.py`): every fact section is stamped with the
  `against_head_sha` (PRs) / `against_updated_at` (issues) it was computed
  against, so it goes stale **automatically** when the PR head / issue moves —
  `is_current()` is the single check, no manual invalidation. The row's `saved_at`
  column is a separate write-stamp that drives the watermark sync below.

## 2. The cockpit reads from an in-memory snapshot

`app/backend/data.py` is the read side. It never does per-request DB
I/O — every board/list read serves from module-level dicts (`_prs`, `_clusters`,
`_pr_to_clusters_idx`).

- **Watermark sync** (`_freshen`): refetches only rows written at or after the
  last `saved_at` watermark, via `store.prs_since` / `store.clusters_since`, and
  atomically rebinds the module globals (a GIL-protected swap — readers never see
  a half-mutated snapshot).
- **Off the request path** (`_ensure`): one blocking cold load on first call;
  after that a background daemon thread refreshes at most once per
  `CHECK_DEBOUNCE` (10s). A slow store can never block or wedge a request — it
  only lets the snapshot lag by up to `CHECK_DEBOUNCE` seconds. `refresh()` (and
  `POST /api/refresh`) force a full reload now.
- **Cluster removals ride the watermark:** a watermark sees inserts/updates but
  not hard-deletes, so `store.delete_cluster` instead **soft-deletes** — it
  tombstones the cluster (a `deleted` flag with a bumped `saved_at`). The tombstone
  flows through the same `store.clusters_since` channel every backend polls, so a
  recluster's removed clusters drop from each operator's snapshot on the next
  freshen, no server bounce. `clusters_since` returns the tombstoned ids
  separately; `_freshen` drops them. Reaping (`store.reap_cluster_tombstones`, run
  at the top of each recluster) hard-removes tombstones long after every reader has
  observed them.

Every `/api/*` response carries `Cache-Control: no-store` so the browser never
caches a stale board on top of an already-debounced snapshot.

## 3. The live sweep reconciles the store with GitHub

The store's PR state is only as fresh as the last phase run, but a PR may have
been closed, merged, reopened, or made conflicting upstream since —
GitHub-owned facts that are the same for every operator.
`app/backend/freshness_live.py` fetches them live and **persists any
drift into the shared store**, so all operators converge on GitHub's truth by
reading the store, not a per-machine cache:

- Read-only upstream (batched GraphQL); the only write is to *our own* store —
  `sweep()` → `persist_live()` writes divergent `meta.state` (open/closed/merged)
  and `signals.mergeable` via `model.Pr.record_live_state`. The executor's own
  merge/close/reopen persist through the same method.
- **Cadence:** on cockpit launch when the last sweep is missing or older than
  `COCKPIT_LIVE_TTL_MIN` (default 60), and on the manual "Refresh live state"
  button. The `live_sweep` singleton row records when it last ran — shared, so one
  operator's sweep gates every cockpit's launch re-sweep and drives the
  "live as of …" UI.
- This is why a PR shown as closed/merged in the cockpit can be fresher than the
  last phase run: the sweep moved it ahead of INGEST, and the next INGEST simply
  re-confirms the same `meta.state`.

## 4. Writes use the configured GitHub App

Reads use the operator's local GitHub login. Two controlled write paths use the
GitHub App named by `TRIAGE_BOT_LOGIN` and mint its token through
`pipeline/get-bot-token.sh`:

- The **executor** (`executor.py` + `safety_guard.py`) uses an explicit command
  allowlist, records every live attempt and dry-run in the SQL `activity` table,
  and routes merges through the dedicated, per-PR-gated `bot_merge_run` path.
- The **chat agent** (`chat.py`) runs a narrower, non-merge `gh` command allowlist
  under `--permission-mode dontAsk` after conversational confirmation.
  Bot-authenticated chat writes and feedback issue filing are not recorded in
  the executor activity table. Resubmit pushes and branch updates run as the
  operator and append best-effort activity entries.

Neither path can write as the app when token minting fails. The trust model in
`CLAUDE.md` is authoritative — read it before anything that writes.

## Gotchas worth knowing before you touch the backend

- **One import mechanism: installed packages.** The source roots are packages —
  `pipeline`, `issue_triage`, and `app` (whose backend is
  `app.backend`), each at its natural path under the repo root —
  installed (editable) via stock setuptools (`pyproject.toml` `[tool.setuptools]`).
  Code imports them qualified: `from pipeline import store`,
  `from app.backend import data`, `from issue_triage import issue_store`.
  The same install backs runtime, pytest, and the CLI, so **green tests imply
  `app.backend.app:app` boots**. A backend-only
  `import app.backend.app` is the quickest boot check. (Standalone tools
  that run under a bare `python3` — `review-new-pr/harness/`, `app/agent/*`
  — bootstrap their own package roots instead.)
- **The store is the joined view.** There is no markdown parsing and no artifact
  joining — read facts from the store, never reconstruct them from `STATUS.md`.
- **JSON only as an escape hatch.** `pipeline/store_migrate.py` /
  `issue_triage/issue_store_migrate.py` `dump`/`import` export and reload the SQL
  store as a tree of JSON files for backup or inspection. They are never on the
  runtime path; nothing in production reads those files.
