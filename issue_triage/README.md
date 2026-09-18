# issue_triage

Store-backed triage pipeline for the open **issues** on the triaged repository
(`TRIAGE_REPO`).
The issue-side counterpart to the PR pipeline uses the same substrate: a
validated store, per-fact freshness, one gate-policy module, a typed
auto-saving domain model, and idempotent phase drivers — so issues are read,
clustered, analyzed, and **kept fresh** exactly the way PRs are.

**Reads run as the operator's local `gh`; the one upstream write (close-as-dup)
goes through the app executor as the configured bot (`TRIAGE_BOT_LOGIN`),
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
- **Links** (`issue_links.py` + `pr_index.py`): the ONE accessor for an issue's
  linked PRs. `linked_prs` merges the issue's stored candidates, GitHub's own
  closing references, and an index of the PR store's `issues.linked` sections
  into one entry per PR under its strongest evidence; the index owns what a PR
  body claims, so a PR opened or edited after the issue's last ingest shows up
  everywhere links are displayed, bundled, or gated on.
- **Gates** (`issue_gates.py`): the ONE policy module. `close_dup_allowed` (pipeline
  auto-recommend) and `close_dup_eligibility` (the app/executor pre-write gate —
  adds a live "canonical open or closed as fixed" check), plus the derived
  `issue_cluster_state`.
  A close-as-dup requires a **confirmed** curation verdict — written by the
  `/diagnose-issue-cluster` agent, not by a human; the human approval is the
  operator's at RESOLVE.
- **Model** (`issue_model.py`): typed `Issue` / `IssueCluster` wrappers; every
  mutator stamps freshness and persists in one validated write. Each phase writes
  only its own section; `Issue.disposition` derives on read — `close-fixed` while
  `fix_scan` cites a merged fixer, else the stored ANALYZE verdict.
- **Taxonomy**: the ONE subsystem-classification accessor in `pipeline/taxonomy.py`
  (vocabulary from the active repository profile, `pipeline/profile.py`; shared with
  PRs so issue↔PR linking lines up).

## Phases (mirror the PR pipeline; each idempotent)

| Phase | Module | What it does |
|---|---|---|
| INGEST | `issue_ingest.py` | fetch open issues (read-only) + compute deterministic `summary` / `repro` / `links` into the store |
| CLUSTER | `issue_cluster_driver.py` | deterministic candidate clusters → membership + pain; flag oversized `needs_review`; **preserve confirmed clusters**, re-cluster only the rest |
| (curate) | `/diagnose-issue-cluster` | agentic: confirm canonical / split false merges → writes the cluster `curation` section |
| ANALYZE | `issue_analyze_driver.py` + `analyze_issues.py` | agentic per-issue disposition (`close-dup` / `request-repro` / `link-pr` / `needs-human`) run in parallel batches and committed back to the store |
| GATE | `issue_gates.py` | close-dup eligibility, computed on read |
| RESOLVE | app `executor.close_issue` | gated close-as-dup upstream as the configured bot |

## Run it

```bash
uv run python issue_triage/issue_pipeline.py              # INGEST + CLUSTER (live fetch)
uv run python issue_triage/issue_pipeline.py --skip-fetch # reuse the store
uv run python issue_triage/analyze_issues.py --limit 200  # ANALYZE the 200 lowest-id pending issues (parallel)
uv run python issue_triage/issue_analyze_driver.py commit verdicts.json
uv run python issue_triage/issue_views.py                 # regenerate ISSUE-STATUS.md / SUMMARY.md
```

The app's Issues tab is a read-only projection over this store
(`prospector_app/backend/issues.py`); the close-as-dup worklist is the confirmed
duplicates, most painful first.

## Issue-fix lane

`fix_lane.py` runs one reported issue through **reproduce → judge → fix → prove
→ review** over single-commit clones of a base this machine already holds. Every
agent is locked down (no GitHub, its own clone its only writable root); only
host-observed sandbox exits and `issue_gates` name the ending. It makes **no
upstream write** — the outcome is a result file plus one ledger row.

```bash
uv run python -m issue_triage.fix_lane --issue N                          # reproduce + fix on the verify pin
uv run python -m issue_triage.fix_lane --issue N --reproduce-only         # stop once the reproduction proves red
uv run python -m issue_triage.fix_lane --issue N --base-sha SHA --tier T  # prove against a base held by hand
```

The base is the verify pin (`prove.pinned`) unless `--base-sha` names one this
machine already holds (`prove.held`, `--tier` defaults to 0); neither builds an
image. The report comes from the issue store, else a live fetch. The run writes
`<verify scratch>/issue-fix/issue-<n>/result.json` and appends one
`issue-fix:run` row to the issue runs ledger.

**Endings.** A verdict exits 0: `reproduced`, `fixed`, `not-reproduced`,
`wrong-symptom`, `not-a-defect`, `unwritable`, `no-fix`, `fix-untrusted`,
`fix-unproven`, `fix-rejected`, `declined`, `cancelled`. A fault — a machine
condition, never a verdict — exits 1: `agent-unavailable`, `run-failed`,
`sandbox`, `base-compile`. Exit 2 is no held base or an unknown issue, and
writes no result file or ledger row.

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
issue→PRs into its PR→issues map. Every display, bundling and gating reader takes
its links from `issue_links.linked_prs`, which reads the PR side's own
`issues.linked` back through `pr_index`. `pain-weights.json` holds the
pain-ranking weights, loaded by `issue_cluster_driver`.

## Skills

- `/diagnose-issue-cluster <N>` — curate one cluster → write its store `curation`
  section (confirm the canonical, split false merges). Read-only on GitHub.
- `/resolve-issue-cluster <N>` — gated upstream execution of close-as-dup via the
  app executor.
