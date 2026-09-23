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
  `issue_cluster_state`; it also holds the issue-fix lane's gates
  (`reproduction_outcome`, `fix_patch_regate`, `fix_proof_bar`).
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

The reproduction agent writes two sets of tests: the reproduction, which must
fail twice on the base, and **preservation tests**, which pin behavior a fix must
keep — the inputs beside the reported one that work today, and what the other
callers of the code at fault rely on — and must pass twice on it. A fix is
proven only when the reproduction turns green and the preservation tests stay
green with it applied. The scope-safety reviewer lists every behavior the fix
alters and marks each as asked for by the report or not; the host reads any
unasked change as unsafe.

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

## History replay

`pipeline/evals/issue_fix_replay.py` measures the fix lane against the past: it
takes closed issues whose merged PR is the known human fix, rebuilds each bug's
pre-fix tree, runs the lane on it, and scores the run against that PR's own tests
as a hidden oracle. It runs entirely on a base this machine already holds and
makes no upstream write.

```bash
uv run python -m pipeline.evals.issue_fix_replay plan             # inspect the corpus, run nothing
uv run python -m pipeline.evals.issue_fix_replay run --limit 10   # score a pilot batch of ten
```

`plan` assembles and screens the corpus and prints each dependency group's
distinct fixing PRs and issues, date span, epoch (its latest merge — the sha to
build the group's base at), and whether it matches the pin — the safety valve to
look before a run. Several issues closed by one PR count as one fix, since
`qualify` measures each fix once. `run` takes the pin's group, runs `--concurrency` (default 2) instances at a
time, and writes a markdown scorecard to `<verify scratch>/replay/<run-id>/table.md`
plus one `replay:instance` ledger row per instance and a `replay:run` summary. A
run is keyed by its base, so re-invoking continues it (`--resume` re-runs the
faulted instances). A failed instance is data, so the run still exits 0. The
pilot's numbers — reproduced, fix rate, oracle pass, false accepts, and wall-time
and agent runs per instance — gate whether the lane goes live.

The **evaluation set** is the frozen yardstick the lane is measured against,
built and run by `pipeline/evals/eval_set.py`:

```bash
uv run python -m pipeline.evals.eval_set build --dry-run   # the plan: groups, fixes, epochs
uv run python -m pipeline.evals.eval_set build             # build until 40 fair bugs (--target)
uv run python -m pipeline.evals.eval_set run --name <run>  # every fair bug x 3 passes, scored
```

`build` harvests every closed issue and merged PR from GitHub (cached under
`<verify scratch>/replay/eval-harvest.json`, `--refresh` to re-read), joins the
issue store's own candidates, and screens and groups them as `plan` does. Each
dependency group, most distinct fixes first, gets one base built at its epoch —
the first commit after the group's last fixing merge whose dependencies still
match, so the base holds every fix and equals none — held against the verify
sweep (`verify_gc.hold`). Its bugs are qualified without an agent, and the
base's verdicts are appended to the runs ledger as an `eval-set:base` row: the
set is deployment data, so it lives in the store, never the tree. A build
resumes past the bases the ledger names.

`run` replays every fair bug through a lane — `--lane staged` (default, the
lane above) or `--lane solo`, the one-agent baseline (`solo_lane.py`: one agent
reproduces, fixes and checks in one clone, and the host's re-gate, green proof
of its tests, compile and related tests alone decide) — `--passes` times (default 3),
each pass its own replay run id `<run>-p<k>`, `--concurrency` instances at a
time, and writes `<verify scratch>/replay/<run>/scorecard.md` plus an
`eval-set:run` ledger row: runs that proposed a fix (ended `fixed`), how many of
those the hidden oracle accepts or refuses only on the maintainers' own contract
(precision), and how many bugs got a proposal at all (coverage). Re-invoking
continues a run; `--resume` re-runs its machine faults.

A PR's tests encode the maintainers' design as well as the bug, so when they
refuse a fix the scorer asks a blind judge (`pipeline/evals/oracle_contract.py`,
shown the report, the PR's test hunks and the failing names, never the fix)
whether each failing test asserts behavior the report asked for. A failure only
the maintainers' own contract explains is `contract_mismatch`, not a
`false_accept`; one the report owns, or the judge cannot place, stays a false
accept. Judgments are cached by their inputs under
`<verify scratch>/replay/oracle-contract/`, so every pass over one bug reads one. Per-run token cost
is a later addition: it needs `fix_lane.LaneResult` to carry the CLI's cost event.

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
