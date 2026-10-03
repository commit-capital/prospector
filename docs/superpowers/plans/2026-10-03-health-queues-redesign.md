# Health & queues redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild Pipeline → Health & queues so it opens on what needs the operator and what every machine did in the past day, with the rest condensed into distinct, foldable panels.

**Architecture:** Every runs-ledger row gains the writing machine's `host`. A new `machine_activity` module folds the past 24 hours of the PR, issue and alert ledgers, the agent ledger, the machine roster and this app's jobs into per-machine counts, served at `/api/machines/activity`. The frontend page splits into one component per panel under `views/control/`, with the sentence wording, the merged run history and the job layout as pure tested modules.

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy store (`pipeline/`, `prospector_app/backend/`), React + TypeScript + Vite (`prospector_app/frontend/`), pytest, `node --test`.

**Spec:** `docs/superpowers/specs/2026-10-03-health-queues-redesign-design.md`

## Global Constraints

- Ledger `host` is additive: `STORE_SCHEMA_VERSION` does not change; a record that already names `host` keeps it.
- Recent activity window: 24 hours, by each row's `finished`, else `started`.
- Worker triggers: `worker`, `autohunt`, `hunter`. Everything else (`None`, `cli`, `operator`) is a person.
- Folded panels remember their state in `localStorage` key `control.panel.<id>`; every access is wrapped in try/catch.
- Comments and docstrings describe the code as it is (CLAUDE.md conventions); every signature typed precisely; no quoted annotations.
- Gates: `uv run pytest`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` (0 errors), `uv run ruff check .` (0 findings), `pnpm run build` and `pnpm exec eslint <touched files>` from `prospector_app/frontend/`, `pnpm test`.
- Never write the shared store from tests or the preview; the preview runs with every worker lane off.

## Review Focus

1. A machine in the roster with no rows and no heartbeat for days — renders `offline <ago> · idle`, not a blank block. (Task 2 test `test_offline_roster_machine_without_rows`.)
2. A ledger row with a malformed or missing `finished` and `started` — skipped, never crashes the endpoint. (Task 2 test `test_row_without_timestamps_is_skipped`.)
3. The same phase name in two ledgers (`ingest` in `pr` and `issue`) — counted as two different jobs. (Task 2 test `test_job_phase_is_keyed_by_ledger`.)
4. Browser storage blocked — panels render at their default open state. (Task 4: `readFold` returns the default when `localStorage` throws; test `fold state falls back when storage throws`.)
5. No suggestions at all — Run a job shows only tiles, no empty "recommended" area. (Task 4 test `no suggestions leaves every job a tile`.)

---

### Task 1: Every ledger row names its machine

**Files:**
- Modify: `pipeline/storekit.py` (add `stamp_host` beside `parse_run`)
- Modify: `pipeline/store.py:660` (`Store.append_run`)
- Modify: `issue_triage/issue_store.py:305` (`IssueStore.append_run`)
- Modify: `alert_triage/alert_store.py:131`, `alert_triage/advisory_store.py:151`
- Test: `pipeline/tests/test_storekit_host.py` (new), plus one assertion each in the issue and alert store tests
- Modify: `CLAUDE.md` (Store bullet)

**Interfaces:**
- Produces: `storekit.stamp_host(record: dict) -> dict` — a copy naming `host`; readers take `record["host"]`, else `record["stats"]["host"]`.

- [ ] **Step 1: Failing test**

```python
from pipeline import settings, storekit
from pipeline.store import Store


def test_stamp_host_adds_the_worker_id(monkeypatch):
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    rec = {"phase": "ingest", "started": None, "finished": None}
    assert storekit.stamp_host(rec) == {**rec, "host": "studio"}
    assert "host" not in rec


def test_stamp_host_keeps_a_named_host(monkeypatch):
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    rec = {"phase": "fix:single", "host": "laptop"}
    assert storekit.stamp_host(rec)["host"] == "laptop"


def test_append_run_stamps_host(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    st = Store(str(tmp_path / "s.db"))
    st.append_run({"phase": "ingest", "started": None, "finished": None})
    assert st.runs()[-1].raw["host"] == settings.worker_id() == "studio"
```

- [ ] **Step 2:** `uv run pytest pipeline/tests/test_storekit_host.py -q` → FAIL (`stamp_host` missing).
- [ ] **Step 3: Implement**

```python
def stamp_host(record: dict) -> dict:
    """`record` naming the machine that writes it: a copy with `host` set to
    this machine's worker id, or `record` itself when it already names one."""
    if record.get("host"):
        return record
    from pipeline import settings
    return {**record, "host": settings.worker_id()}
```

In each of the four `append_run`s, first line after the docstring: `record = storekit.stamp_host(record)`.

- [ ] **Step 4:** Add to the issue store test file and the alert store test file a test appending a run and asserting `raw["host"]`. Run `uv run pytest pipeline/tests issue_triage/tests alert_triage/tests -q`; fix any test that compared a ledger record exactly by adding `host` to its expectation.
- [ ] **Step 5:** CLAUDE.md Store bullet: append "Every runs-ledger row names the machine that wrote it (`host`, `storekit.stamp_host`)."
- [ ] **Step 6:** Commit `Name the writing machine on every runs-ledger row`.

### Task 2: `/api/machines/activity`

**Files:**
- Create: `prospector_app/backend/machine_activity.py`
- Modify: `prospector_app/backend/app.py` (route beside `/api/machines`)
- Test: `prospector_app/backend/tests/test_machine_activity.py`

**Interfaces:**
- Consumes: `storekit.stamp_host` rows; `machines.roster() -> {machines: [{host, lanes, beats, online, ...}], local}`; `jobs.list_jobs() -> list[JobView]`; `jobs.JOB_SPECS[kind]["ledger"] -> (ledger, phases)`.
- Produces: `summarize(rows: list[tuple[str, dict]], agent_runs: list[dict], roster: dict, local_jobs: list[dict], job_phases: dict[tuple[str, str], tuple[str, str]], now: datetime) -> ActivityView`; `activity() -> ActivityView`.

```
ActivityView = {local: str, window_hours: int, machines: [MachineActivity]}
MachineActivity = {host, local: bool, online: bool, offline_since: str | None, has_worker: bool,
  tripped: [str], current: {pr: int | None, issue: int | None},
  lanes: {"security"|"verify"|"autofix"|"issue_fix": {count, numbers: [int], outcomes: [{label, count, numbers}]}},
  background: [{label, count}], jobs: [{label, kind, status, job_id: int | None}], spend_usd: float}
```

- [ ] **Step 1: Failing tests** — one per behavior, over plain dicts, `now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)`:
  - `test_lane_outcomes` — security verdicts, verify `done`/`waiting-for-base`/`error` mapping, autofix `awaiting-review` → `parked`, issue-fix `ending`; outcomes sorted by count desc; `numbers` distinct.
  - `test_window_edge` — a row finished 24h01m ago is out, 23h59m in.
  - `test_row_without_timestamps_is_skipped`.
  - `test_host_from_stats_then_unattributed` — `stats.host` is read; no host lands in `unattributed`, listed last.
  - `test_background_labels_and_worker_cluster_rows` — `ingest:watch` → `PR watches`; `cluster:summaries` with `trigger: worker` → background, with `trigger: cli` → job.
  - `test_job_phase_is_keyed_by_ledger` — `("pr","ingest")` → Ingest, `("issue","ingest")` → Issue ingest, deduped by label per machine.
  - `test_local_jobs_replace_ledger_jobs` — the local host's jobs come from `local_jobs` (in window, status kept, `job_id` set).
  - `test_offline_roster_machine_without_rows` — `online False`, `offline_since` = newest beat, empty lanes.
  - `test_spend_sums_agent_runs_per_host`.
  - `test_route_answers` — FastAPI `TestClient` on `/api/machines/activity` with `machine_activity.activity` monkeypatched.
- [ ] **Step 2:** run → FAIL (module missing).
- [ ] **Step 3: Implement** `machine_activity.py`:

```python
LANE_PHASES = {"security:review-one": "security", "verify:single": "verify",
               "fix:single": "autofix", "issue-fix:run": "issue_fix"}
BACKGROUND = {"ingest:watch": "PR watches", "threat-scan:heads": "threat scans",
              "rereview:request": "re-review requests", "verify:pin-refresh": "base pin refreshes",
              "reingest": "re-ingests", "threat-evidence:capture": "evidence captures"}
WORKER_BACKGROUND = {"cluster:summaries": "summary batches", "cluster:assign": "cluster placements",
                     "analyze:commit": "cluster analyses"}
WORKER_TRIGGERS = frozenset({"worker", "autohunt", "hunter"})
UNATTRIBUTED = "unattributed"
WINDOW_HOURS = 24
```

`_row_host(raw)`: `raw.get("host") or (raw.get("stats") or {}).get("host")`, as `str`, else None. `_when(raw)`: `storekit.parse_ts(raw.get("finished") or raw.get("started"))`. `_outcome(lane, stats)` per the spec table. `_number(lane, raw)`: `raw.get("issue")` for `issue_fix`, else `raw.get("pr")`, kept only when `int`. Machines from the roster first (online, `offline_since` = max `last_beat` over beats when not online, `has_worker` = any beats, tripped lanes, current PR/issue from an online beat), then every host seen in rows; sort `(not online, host)` with `unattributed` last. `activity()` gathers `data.runs(since=cutoff)` as `("pr", raw)`, `issues.cached_runs()` as `("issue", raw)`, `alert_data.runs()` as `("alert", raw)` (PhaseRun only, `.raw`), `data.store().agent_runs(cutoff)`, `machines.roster()`, `jobs.list_jobs()`, and `job_phases` from `JOB_SPECS` (every `(ledger, phase)` not in `LANE_PHASES` → `(kind, label)`).

Route:

```python
@app.get("/api/machines/activity")
def machines_activity() -> machine_activity.ActivityView:
    """What every machine did in the past day, with its status — the Control
    tab's Recent activity panel."""
    return machine_activity.activity()
```

- [ ] **Step 4:** `uv run pytest prospector_app/backend/tests/test_machine_activity.py -q` → PASS; pyright on the module.
- [ ] **Step 5:** Commit `Serve what each machine did in the past day`.

### Task 3: Recommended jobs for the Control tab

**Files:**
- Modify: `prospector_app/backend/suggested_actions.py` (`pr_suggestions`, `suggestions`, `VIEWS`)
- Test: `prospector_app/backend/tests/test_suggested_actions.py`

**Interfaces:**
- Produces: `suggestions("all") -> list[Suggestion]` (prs + issues + alerts, deduped by `kind`, first wins); `pr_suggestions` adds `cluster-new`.

- [ ] **Step 1: Failing tests**

```python
def test_cluster_new_when_unclustered_and_clustering_stale():
    status = {"phases": [{"phase": "cluster", "last_run": "2026-09-01T00:00:00+00:00"}],
              "coverage": {"not_clustered": 826, "threat": {}, "analysis": {}}, "estimates": {}}
    s = [x for x in suggested_actions.pr_suggestions(status, NOW) if x["kind"] == "cluster-new"]
    assert s and s[0]["reason"].startswith("826 PRs are not in a cluster") and s[0]["count"] == 20


def test_no_cluster_new_after_recent_clustering():
    status = {"phases": [{"phase": "cluster", "last_run": iso(NOW - 3600)}],
              "coverage": {"not_clustered": 5, "threat": {}, "analysis": {}}, "estimates": {}}
    assert not [x for x in suggested_actions.pr_suggestions(status, NOW) if x["kind"] == "cluster-new"]
```

(Use the module's existing test helpers for `NOW`/`iso` if present.) Check `pipeline_status.status()["phases"]` for the clustering phase's name first and use it.

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement** in `pr_suggestions` after the threat-scan block:

```python
    unclustered = cov.get("not_clustered", 0)
    cluster_last = last_runs.get("cluster")
    if unclustered > 0 and _stale(cluster_last, now_ts):
        out.append({
            "kind": "cluster-new", "title": "Cluster new PRs",
            "reason": f"{unclustered} PR{'s are' if unclustered != 1 else ' is'} not in a cluster "
                      f"({_ago(cluster_last, now_ts).replace('last run', 'last clustering')})",
            "last_run": cluster_last, "count": CLUSTER_BATCH, "estimate_seconds": None,
        })
```

`suggestions("all")` concatenates the three views, dropping a repeated `kind`.

- [ ] **Step 4:** run → PASS. Commit `Recommend clustering when PRs sit unclustered`.

### Task 4: Frontend foundations — types, format helpers, Panel, pure modules

**Files:**
- Modify: `prospector_app/frontend/src/api.ts` (`MachineActivity`, `ActivityView`, `api.machinesActivity`, `SuggestedActionView` gains `"all"`)
- Create: `src/views/control/format.ts` (moved `ago`, `fmt`, `fmtDuration`, `fmtElapsed`, `agoColor`, `resultChip`, `fixStatusChip`, `queueStatusChip`, `elapsed`, `localTime`, `ageText`, `percent`)
- Create: `src/views/control/Panel.tsx`, `src/views/control/fold.ts` (+ `fold.test.ts`)
- Create: `src/views/control/activitySentence.ts` (+ test), `runHistory.ts` (+ test), `jobLayout.ts` (+ test)
- Modify: `src/styles.css` (`.cpanel*`, `.job-tiles`, `.queue-cards`, `.activity-machine`)

**Interfaces:**
- `readFold(id: string, fallback: boolean, storage?: Pick<Storage, "getItem">): boolean`; `writeFold(id: string, open: boolean, storage?: Pick<Storage, "setItem">): void`.
- `<Panel id title meta? tone?: "danger" | "accent" foldable? defaultOpen? summary? children />`.
- `activityPhrases(m: MachineActivity): ActivityPhrase[]` where `ActivityPhrase = {key: string; lead: PhrasePart; parts: PhrasePart[]; style: "paren" | "list" | "plain"}`, `PhrasePart = {text: string; numbers?: number[]; jobId?: number; bad?: boolean}`; `machineStatus(m, now: number): {text: string; tone: "ok" | "bad" | "muted"}[]`.
- `mergeRuns(hunt: AutohuntRun[], fix: AutohuntRun[]): AutohuntRun[]` (newest first by `finished ?? started`); `countResults(rows: AutohuntRun[]): Record<"security" | "verify" | "fix", Record<string, number>>`.
- `layoutJobs<S extends {kind: string}>(specs: S[], suggestions: SuggestedAction[]): {recommended: {spec: S; suggestion: SuggestedAction}[]; rest: S[]}`.

- [ ] **Step 1: Failing tests** (`node --test`, importing `./x.ts`):
  - activitySentence: `"8 security reviews"` lead with outcome parts `"4 GREEN"`; `1 security review` singular; `failed`/`error…`/`RED` parts `bad: true`; background renders as `list` with lead `background:`; local jobs lead `you ran`, remote `ran`, failed job `bad`; spend `$41 AI` rounded; no lanes, background, jobs or spend → `[]`.
  - machineStatus: online → `● online`; offline with `offline_since` 3h ago → `offline 3h`; tripped `["security"]` → `security paused` bad; `has_worker false` and offline → `no worker` muted; `unattributed` → `machine not recorded` muted.
  - runHistory: merge order, `countResults` per lane.
  - jobLayout: recommended order follows suggestions; unknown kinds ignored; duplicate kinds once; `no suggestions leaves every job a tile`.
  - fold: `fold state falls back when storage throws`; reads `"1"`/`"0"`.
- [ ] **Step 2:** `pnpm test` → FAIL.
- [ ] **Step 3:** Implement the modules (pure, `import type` only), Panel (header band `cpanel-head`, body `cpanel-body`, caret when foldable, `summary` shown in the header while folded), CSS:

```css
.cpanel { border: 1px solid var(--border); border-radius: 10px; margin: 0 0 16px; overflow: hidden; background: var(--bg); }
.cpanel-head { display: flex; align-items: baseline; gap: 10px; padding: 9px 14px; background: var(--panel); border-bottom: 1px solid var(--border); }
.cpanel.folded .cpanel-head { border-bottom: none; }
.cpanel-title { font-size: 14px; font-weight: 600; margin: 0; }
.cpanel-meta { color: var(--muted); font-size: 12px; min-width: 0; }
.cpanel-body { padding: 12px 14px; }
.cpanel.tone-danger { border-color: var(--red); }
.cpanel.tone-danger .cpanel-head { background: var(--reason-tint); }
.cpanel.tone-danger .cpanel-title { color: var(--red); }
.cpanel.tone-accent { border-color: var(--accent); }
.cpanel-fold { all: unset; cursor: pointer; display: flex; align-items: baseline; gap: 10px; flex: 1; min-width: 0; }
.job-tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 8px; }
.queue-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
```

- [ ] **Step 4:** `pnpm test` → PASS; `pnpm exec tsc -b` clean. Commit `Add the Control tab's panel, sentence, history and job-layout pieces`.

### Task 5: Rebuild the page from panels

**Files:**
- Create: `src/views/control/NeedsYou.tsx` (from `ControlPanel.tsx` `TrippedLane`, `HostBase`, the needs-attention chips), `RecentActivity.tsx`, `Queues.tsx` (verify + fix queue tables moved verbatim with `RunningMeta`, `PARKED_READY`, approve/discard/escalate), `Coverage.tsx` (`PhaseCard` + ingest card), `Jobs.tsx`, `useJobRunner.ts` (the `attachStream`/`run`/`stopJob` state from `ControlPanel`), `JobLine.tsx` (`JobConsoleStatus` + console), `Capacity.tsx` (`CapacityBar`, `CapacityAccountCard`, one-line `capacitySummary`), `RunHistory.tsx` (`HuntLaneSummary`, merged table, one range picker)
- Modify: `src/views/ControlPanel.tsx` → page shell: owns polls (`autohunt(range)`, `verifyQueue`, `fixQueue(range)`, `capacity`, `machinesActivity`, `pipelineStatus`, `suggestedActions("all")`, `jobSpecs`, `jobRuntimes`), renders `JobLine`, then panels in spec order.
- Modify: `src/views/Tables.tsx` (decision-capture box at the foot, fetching `api.trainingStats()`)

- [ ] **Step 1:** Move code into the panel files; delete `MachinesSection`, the worker-health box, the Recent jobs table, the intro paragraph and the separate verify run history.
- [ ] **Step 2:** NeedsYou lines: tripped lanes (Resume + expander with reason, remedy, self-test), offline machines with a worker (`online false`, `has_worker`), failing or stale base pins, paused AI accounts, failed-run groups (`N failed runs across M reasons` expanding the existing chips). None → one `All lanes healthy` line, panel untoned.
- [ ] **Step 3:** RecentActivity renders each machine: name, `this machine` chip, `machineStatus` items, current PR/issue links, then `activityPhrases` (`paren`: `lead (a, b)`, `list`: `lead a, b`, `plain`: muted) joined by ` · `, or `idle`. Lead/parts with `numbers` and a PR lane link to `/prs/list?spec=` `{numbers, state: "all"}`; parts with `jobId` call `onOpenJob(jobId)`.
- [ ] **Step 4:** Queues: three cards (Security pool; Verify running/queued/waiting + GREEN pool; Fix running/awaiting review/recently ended) — Verify and Fix toggle their table beneath, one open at a time; Fix opens on first load when any row is `awaiting-review`.
- [ ] **Step 5:** Jobs: `layoutJobs(specs, suggestions)`; recommended rows full width (`recommended` chip, reason, detail, inputs, last run, duration, Run); `rest` plus Refresh live state and Scan responses as `.job-tiles` buttons; clicking one opens it full width under the grid (one at a time); the running job's tile shows `running…`.
- [ ] **Step 6:** `pnpm run build` (0 tsc errors), `pnpm exec eslint src/views/ControlPanel.tsx src/views/control src/views/Tables.tsx src/api.ts`, `pnpm test`.
- [ ] **Step 7:** Commit `Rebuild Health & queues as distinct panels led by recent activity`.

### Task 6: Verify in the app

- [ ] **Step 1:** Read `.env`/`.claude/launch.json`; start the preview with `TRIAGE_VERIFY_WORKER`, `TRIAGE_FIX_WORKER`, `TRIAGE_ISSUE_FIX_WORKER`, `TRIAGE_CLUSTER_WORKER` and `TRIAGE_PR_WATCH` off for the preview process.
- [ ] **Step 2:** Load `/pipeline/control`: no console errors, `/api/machines/activity` 200, every panel renders, folding persists across reload, a tile opens, Fix card table toggles.
- [ ] **Step 3:** Screenshot for the PR.

### Task 7: Ship

- [ ] Full gates (pytest, pyright, ruff, frontend build/lint/test); fresh-context review of the branch; push; PR; CI green; merge.
