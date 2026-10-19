# Architecture — the data layer

How a fact gets from GitHub into the store and out to an API request: the SQL
store, the app's in-memory snapshot, and the live sweep. The trust model is in
`AGENTS.md`, and the phase commands are in `pipeline/workflows/README.md`. The
source files named here hold the detail.

```
 GitHub ──INGEST/phases──►  SQL store  ──watermark sync──►  app snapshot  ──►  /api/* reads
   ▲    ▲                  (storekit)     (data.py)          (in-memory dicts)
   │    │ live sweep            │
   │    └ (freshness_live) ─────┤  persists PR closed/merged/mergeable
   │      GraphQL, read-only    │  drift straight into meta.state / signals
   └──────── executor / chat write upstream as configured bot ◄────┘
```

## 1. The store is SQL

One database is the source of truth. Status and briefing Markdown is generated
output, never parsed back.

- **Accessor:** `pipeline/store.py` over `pipeline/storekit.py`;
  `issue_triage/issue_store.py` and `alert_triage/alert_store.py` sit on the same
  core.
- **Schema** (`pipeline/schema.py`): a row per PR and per cluster, with fact
  sections in a JSON `data` column beside mirror columns and a `saved_at`
  write-stamp; a `runs` ledger; singleton `registries` rows; the app's
  `activity` and `chat_messages` tables; and the issue and alert tables.
- **Backend** (`storekit.resolve_url`): `--store DIR` → SQLite in `DIR`; else
  `TRIAGE_STORE_URL`; else a local SQLite file under the store's directory. On
  Supabase use the transaction pooler (port 6543); `storekit.get_engine` is
  configured for it.
- **Freshness** (`pipeline/freshness.py`, `issue_triage/issue_freshness.py`):
  each fact section is stamped with the head SHA or issue update time it was
  computed against, and `is_current()` decides whether it still holds. There is
  no manual invalidation.

## 2. The app reads from an in-memory snapshot

`prospector_app/backend/data.py` serves every read from module-level dicts and
never touches the database on the request path.

- **Watermark sync** (`_freshen`): fetches only rows saved since the last
  `saved_at` watermark (`store.prs_since`, `store.clusters_since`) and swaps the
  dicts atomically.
- **Off the request path** (`_ensure`): one cold load at boot, then a
  background refresh at most every `CHECK_DEBOUNCE`. A slow store only lets the
  snapshot lag; `/api/health` answers `loading` during the cold load.
- **Cold load** (`_cold_load`): starts from this machine's disk copy
  (`snapshot_cache`) and reads only what changed since it. Without a copy it
  reads light records (`store.prs_light`), then fills in the long text in the
  background. A cut string is a `storekit.Clipped`, which the store refuses to
  save.
- **Ledgers** (`run_ledger.RunLedger`): the run ledgers and the activity log
  are held in memory, read incrementally by rowid, and copied to disk the same
  way.
- **Cluster removals** are soft deletes, so they ride the watermark:
  `store.delete_cluster` tombstones the row, `_freshen` drops it, and
  `store.reap_cluster_tombstones` removes old tombstones at each recluster.

Every `/api/*` response carries `Cache-Control: no-store`.

## 3. The live sweep reconciles the store with GitHub

`prospector_app/backend/freshness_live.py` reads PR state from GitHub (batched
GraphQL) and persists drift in `meta.state` and `signals.mergeable` into the
shared store through `model.Pr.record_live_state`, the same method the executor
uses after its own writes. It runs at app launch when the last sweep is older
than `PROSPECTOR_LIVE_TTL_MIN`, and on the "Refresh live state" button. That is
why the app can show a PR closed before the next INGEST.

## Before you touch the backend

- A backend-only `import prospector_app.backend.app` is the quickest boot
  check, and green tests imply the app boots: runtime, pytest and the CLI share
  one editable install.
- `pipeline/store_migrate.py` and `issue_triage/issue_store_migrate.py` dump and
  reload the store as JSON for backup or inspection. Nothing at runtime reads
  those files.
