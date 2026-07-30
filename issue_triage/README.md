# issue_triage

Store-backed triage pipeline for the open **issues** on the triaged repository
(`TRIAGE_REPO`).
The issue-side counterpart to `pipeline/` (which does PRs): it runs on the same v2
substrate — a validated store, per-fact freshness, one gate-policy module, a typed
auto-saving domain model, and idempotent phase drivers — so issues are read,
clustered, analyzed, and **kept fresh** exactly the way PRs are.

**Reads run as the operator's local `gh`; the one upstream write (close-as-dup)
goes through the cockpit executor as the configured bot (`TRIAGE_BOT_LOGIN`),
gated and logged.**

## Substrate (shared with the PR pipeline)

- **Store** (`issue_store.py`): a SQL database — one row per issue (its sections
  `meta / summary / repro / cluster / analysis / links / resolution` carried in a
  JSON `data` column), one row per issue-cluster, and a `runs` ledger table.
  `TRIAGE_STORE_URL` (a shared SQL database) or a local SQLite default
  under `issue_triage/store/`. **Validated on write; the ONLY accessor** — never
  hand-write rows. Built on the shared `pipeline/storekit.py` core.
- **Freshness** (`issue_freshness.py`): every fact section is stamped
  `against_updated_at`. When an issue's `meta.updated_at` moves (GitHub bumps it on
  edit/comment/label/state change — the analog of a PR's `head_sha`), its
  summary/repro/analysis go stale **automatically**; `is_current()` is the single
  check.
- **Gates** (`issue_gates.py`): the ONE policy module. `close_dup_allowed` (pipeline
  auto-recommend) and `close_dup_eligibility` (the cockpit/executor pre-write gate —
  adds a live "canonical open or closed as fixed" check), plus the derived
  `issue_cluster_state`.
  A close-as-dup requires a **human-confirmed** curation verdict.
- **Model** (`issue_model.py`): typed `Issue` / `IssueCluster` wrappers; every
  mutator stamps freshness and persists in one validated write.
- **Taxonomy**: the ONE subsystem-classification accessor in `pipeline/taxonomy.py`
  (vocabulary from the active repository profile, `pipeline/profile.py`; shared with
  PRs so issue↔PR linking lines up).

## Phases (mirror the PR pipeline; each idempotent)

| Phase | Module | What it does |
|---|---|---|
| INGEST | `issue_ingest.py` | fetch open issues (read-only) + compute deterministic `summary` / `repro` / `links` into the store |
| CLUSTER | `issue_cluster_driver.py` | deterministic candidate clusters → membership + pain; flag oversized `needs_review`; **preserve human-confirmed clusters**, re-cluster only the rest |
| (curate) | `/diagnose-issue-cluster` | agentic: confirm canonical / split false merges → writes the cluster `curation` section |
| ANALYZE | `issue_analyze_driver.py` + `analyze_issues.py` | agentic per-issue disposition (`close-dup` / `request-repro` / `link-pr` / `needs-human`) run in parallel batches and committed back to the store |
| GATE | `issue_gates.py` | close-dup eligibility, computed on read |
| RESOLVE | cockpit `executor.close_issue` | gated close-as-dup upstream as the configured bot |

## Run it

```bash
python issue_triage/issue_pipeline.py              # INGEST + CLUSTER (live fetch)
python issue_triage/issue_pipeline.py --skip-fetch # reuse the store
python issue_triage/analyze_issues.py --limit 200  # ANALYZE the 200 lowest-id pending issues (parallel)
python issue_triage/issue_analyze_driver.py commit verdicts.json   # apply verdicts from a file (manual path)
python issue_triage/issue_views.py                 # regenerate ISSUE-STATUS.md / SUMMARY.md
```

The cockpit's Issues tab is a read-only projection over this store
(`app/backend/issues.py`); the close-as-dup worklist is the confirmed
duplicates, most painful first.

## Pain score

Balanced normalized blend of **distinct reporters · reactions/👍 · comments**,
times a **severity multiplier** (keyword + label scan). Duplicates are *signal*: a
cluster's reactions and comments are summed across all members, so re-filed dupes
raise the canonical's rank — but breadth counts each author once, so a single
prolific filer is one vote, not many. Weights live in `pain-weights.json`; the
blend math is in `pain_score.py` (reused by `issue_cluster_driver`).

## Stage modules

The deterministic stage modules (`fetch_issues`, `summarize_issues`, `cluster_issues`,
`repro_grade`, `pain_score`, `link_prs`) are pure-function libraries that the SQL-store
drivers (`issue_ingest`, `issue_cluster_driver`) import — `fetch_all`, `summarize`,
`classify_subsystem`, `cluster_issues`, `grade_repro`, `pain_for_cluster`,
`candidate_prs`. They write nothing to disk.

`issue_ingest` writes each issue's candidate PRs into the store (`set_links`); the
PR pipeline (`pipeline/ingest.py:load_issue_links`) reads them from there, inverting
issue→PRs into its PR→issues map. `pain-weights.json` holds the pain-ranking
weights, loaded by `issue_cluster_driver`.

## Skills

- `/diagnose-issue-cluster <N>` — curate one cluster → write its store `curation`
  section (confirm the canonical, split false merges). Read-only on GitHub.
- `/resolve-issue-cluster <N>` — gated upstream execution of close-as-dup via the
  cockpit executor.
