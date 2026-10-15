# Health & queues PR 1 — layout and overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild Pipeline → Health & queues around one overview read: a status line, a Needs you list holding only real problems, a Lanes table (state per machine, now, next, 24h), a Timeline, and tabs for the secondary panels. Add a per-machine "stood down" mark so a machine stopped on purpose is never an alarm.

**Architecture:** A new pure module `control_overview.py` (`build(inputs, now)`) folds data the store already holds, plus two new per-host registry rows: `machine_mode:<host>` (stood down) and `worker_loops:<host>` (which lanes the machine runs, and when each periodic loop last ran). `gather()` reads the live inputs; `GET /api/control/overview` serves it. The page polls that one endpoint and renders four new components. `system_health.summarize` takes the stood-down hosts too, so the red strip on every page agrees with the page.

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy store (`pipeline/store.py`), pytest; React + TypeScript (Vite), `node --test` for pure TS helpers.

**Spec:** `docs/superpowers/specs/2026-10-14-health-queues-lanes-design.md`

## Spec refinements made here

These narrow PR 1. Task 0 writes them into the spec so the two documents agree.

- Lane flags travel in the `worker_loops:<host>` row, not in each heartbeat. The same row records when each periodic loop last ran, so it is one write per loop pass.
- "Hunter's next picks" moves to PR 2, alongside reorder. In PR 1 the Next cell shows the queue head, or else the hunter pool size.
- Timeline lines do not open job output. Jobs started from this app still show in the running-job line.
- The stand-down toggle lives on this page only (the Needs you offline row and the status line), not also in Setup → Machines.

## Global Constraints

- Every module starts with `from __future__ import annotations`. Signatures are typed precisely: no bare `list` or `dict` where the element type is known, and no quoted annotations.
- `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` stays at 0 errors, and `uv run ruff check .` stays at 0 findings.
- Imports are qualified (`from prospector_app.backend import …`). Never use `sys.path.insert`.
- Comments describe the code as it is now. No "previously", no "instead of".
- Store rows are written only through `Store` methods. No new environment variable is read, so `settings_registry` does not change.
- No `STORE_SCHEMA_VERSION` bump: the new rows are new registry names that older code never reads.
- Frontend: `pnpm run build` passes with 0 tsc errors; `pnpm exec eslint <touched files>` adds no errors; use pnpm, never npm. Strings are double-quoted and prop types inline.
- Every `localStorage` access is wrapped in try/catch, and the page renders correctly without storage.
- Tests: `uv run pytest <path>` for Python; `pnpm test` from `prospector_app/frontend` for TS helpers.
- Never point a preview from this worktree at the shared store (memory: a Studio worktree `.env` starts live workers). Verify against a scratch store.

## Review Focus

1. **A host with no `worker_loops` row** (an older checkout still running) should read its lanes from its heartbeats, not as all-off. Covered by `test_lanes_fall_back_to_beats_without_loops_row` in Task 4.
2. **A stood-down machine that comes back** — any beat after the mark — should read as working, with no click needed. Covered by `test_beat_after_mark_ends_stand_down` in Task 2.
3. **A loop on an offline machine** should never be shown as "next run in 6m". Covered by `test_loops_on_offline_host_have_no_next` in Task 4.
4. **A muted item reappears after 24h**, and a page with storage blocked never hides anything. Covered by `mute.test.ts` in Task 6.
5. **A machine that is stood down and also tripped** must not raise a red strip on other pages. Covered by `test_stood_down_host_drops_out_of_strip` in Task 3.

---

## File structure

| File | Responsibility |
|---|---|
| `pipeline/store.py` (modify) | `machine_mode:<host>` and `worker_loops:<host>` row accessors |
| `pipeline/tests/test_store_machine_rows.py` (create) | those accessors |
| `prospector_app/backend/machine_mode.py` (create) | stand down / stand up, and `stood_down(modes, roster)` |
| `prospector_app/backend/worker_loops.py` (create) | this host's lanes-on map and loop stamps, written to its row |
| `prospector_app/backend/control_overview.py` (create) | pure `build()` plus `gather()` |
| `prospector_app/backend/system_health.py` (modify) | drop stood-down hosts |
| `prospector_app/backend/app.py` (modify) | the 3 endpoints; publish loops at startup and after `/api/setup/flags` |
| `prospector_app/backend/{threat_refresh,pr_watch,stale_refresh,rereview_hunt,cluster_refresh,issue_fix_worker}.py` (modify) | stamp each loop pass |
| `prospector_app/backend/tests/test_machine_mode.py`, `test_worker_loops.py`, `test_control_overview.py` (create); `test_system_health.py` (modify) | tests |
| `prospector_app/frontend/src/api.ts` (modify) | `ControlOverview` types and calls |
| `prospector_app/frontend/src/views/control/mute.ts` + `mute.test.ts` (create) | per-browser 24h mute |
| `prospector_app/frontend/src/views/control/overviewText.ts` + `overviewText.test.ts` (create) | pure wording for states, next and the status line |
| `prospector_app/frontend/src/views/control/{StatusLine,Attention,Lanes,Timeline}.tsx` (create) | the four sections |
| `prospector_app/frontend/src/views/ControlPanel.tsx` (rewrite) | layout and polling |
| `prospector_app/frontend/src/views/control/Queues.tsx` (modify) | export `FixTable` and `VerifyTable`; delete `Queues` |
| `prospector_app/frontend/src/views/control/{NeedsYou,RecentActivity}.tsx` (delete) | replaced |
| `prospector_app/frontend/src/styles.css` (modify) | `.lanes`, `.timeline`, `.ctabs` rules |

---

### Task 0: Record the refinements in the spec

**Files:** Modify `docs/superpowers/specs/2026-10-14-health-queues-lanes-design.md`

- [ ] **Step 1: Edit the spec**

  - In "Overview endpoint", replace the sentence beginning "Each worker heartbeat adds the lane flags" with: "Each machine writes a `worker_loops:<host>` row (`worker_loops.py`) holding which lanes it runs and each periodic loop's last pass, so a remote machine's `off` and its loops' next runs are known rather than guessed."
  - In "Lanes", under the expanded row, append to the Hunter's next picks bullet: "(PR 2)".
  - In "Timeline", replace "or its job output as today (`jobToOpen`)" with "Jobs started from this app show in the running-job line."
  - In "Machines off on purpose", delete the sentence "The machine block in Setup → Machines offers the same toggle."
  - In "Delivery" PR 2, add "the hunter's next picks".

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-10-14-health-queues-lanes-design.md
git commit -m "Narrow the Health & queues spec to what PR 1 builds"
```

---

### Task 1: Store rows for machine mode and worker loops

**Files:**
- Modify: `pipeline/store.py`, beside `save_worker_health` (around line 1355)
- Test: `pipeline/tests/test_store_machine_rows.py`

**Interfaces — produces:**
- `Store.save_machine_mode(record: dict) -> None` (requires `record["host"]`)
- `Store.load_machine_modes() -> dict[str, dict]` (host → record)
- `Store.clear_machine_mode(host: str) -> bool`
- `Store.save_worker_loops(record: dict) -> None` (requires `record["host"]`)
- `Store.load_worker_loops() -> dict[str, dict]`

- [ ] **Step 1: Write the failing test**

```python
"""Per-host machine-mode and worker-loops registry rows."""
from __future__ import annotations

import pytest

from pipeline import store as S


@pytest.fixture
def st(tmp_path):
    return S.Store(tmp_path / "store")


def test_machine_mode_round_trips_and_clears(st):
    st.save_machine_mode({"host": "mac-1", "stood_down": True, "at": "2026-10-14T10:00:00+00:00",
                          "by": "ops", "note": "laptop"})
    assert st.load_machine_modes() == {"mac-1": {"host": "mac-1", "stood_down": True,
                                                 "at": "2026-10-14T10:00:00+00:00",
                                                 "by": "ops", "note": "laptop"}}
    assert st.clear_machine_mode("mac-1") is True
    assert st.load_machine_modes() == {}
    assert st.clear_machine_mode("mac-1") is False


def test_worker_loops_one_row_per_host(st):
    st.save_worker_loops({"host": "a", "at": "t1", "lanes": {"verify": True}, "loops": {}})
    st.save_worker_loops({"host": "b", "at": "t2", "lanes": {}, "loops": {}})
    st.save_worker_loops({"host": "a", "at": "t3", "lanes": {"verify": False}, "loops": {}})
    rows = st.load_worker_loops()
    assert set(rows) == {"a", "b"}
    assert rows["a"]["at"] == "t3" and rows["a"]["lanes"] == {"verify": False}


def test_rows_require_a_host(st):
    with pytest.raises(S.ValidationError):
        st.save_machine_mode({"stood_down": True})
    with pytest.raises(S.ValidationError):
        st.save_worker_loops({"lanes": {}})


def test_new_rows_do_not_leak_into_worker_health(st):
    st.save_machine_mode({"host": "mac-1", "stood_down": True, "at": "t"})
    st.save_worker_loops({"host": "mac-1", "at": "t", "lanes": {}, "loops": {}})
    assert st.load_worker_health() == {"hosts": {}}
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest pipeline/tests/test_store_machine_rows.py -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'save_machine_mode'`.

- [ ] **Step 3: Implement**

Add this to `Store`, after `clear_worker_health`. `ValidationError` is already imported in `store.py`; `ColumnElement` and `Iterable` already appear in its annotations.

```python
    _MODE_PREFIX = "machine_mode:"
    _LOOPS_PREFIX = "worker_loops:"

    def _rows_by_host(self, prefix: str) -> dict[str, dict]:
        from sqlalchemy import select
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(schema.registries.c.name, schema.registries.c.data)
                .where(schema.registries.c.name.like(f"{prefix}%"))).all()
        return {name[len(prefix):]: rec for name, rec in rows}

    def save_machine_mode(self, record: dict) -> None:
        """An operator's mark on one machine, keyed by `host`: `{host,
        stood_down, at, by, note}`."""
        if not record.get("host"):
            raise ValidationError("machine_mode.host: required")
        self._save_registry(f"{self._MODE_PREFIX}{record['host']}", record)

    def load_machine_modes(self) -> dict[str, dict]:
        return self._rows_by_host(self._MODE_PREFIX)

    def clear_machine_mode(self, host: str) -> bool:
        return self._delete_registry(f"{self._MODE_PREFIX}{host}")

    def save_worker_loops(self, record: dict) -> None:
        """One machine's lanes and periodic-loop passes, keyed by `host`:
        `{host, at, lanes: {lane: bool}, loops: {name: {last_at, every_seconds}}}`."""
        if not record.get("host"):
            raise ValidationError("worker_loops.host: required")
        self._save_registry(f"{self._LOOPS_PREFIX}{record['host']}", record)

    def load_worker_loops(self) -> dict[str, dict]:
        return self._rows_by_host(self._LOOPS_PREFIX)
```

Check that `_HEALTH_PREFIX` (`grep -n "_HEALTH_PREFIX =" pipeline/store.py`) does not match `machine_mode:` or `worker_loops:`. It is `worker_health:`, and the last test above proves the two kinds of row stay apart.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest pipeline/tests/test_store_machine_rows.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/store.py pipeline/tests/test_store_machine_rows.py
git commit -m "Store per-host machine-mode and worker-loops rows"
```

---

### Task 2: Stand a machine down

**Files:**
- Create: `prospector_app/backend/machine_mode.py`
- Modify: `prospector_app/backend/app.py` (after `machines_activity`, around line 322)
- Test: `prospector_app/backend/tests/test_machine_mode.py`

**Interfaces:**
- Consumes: the Task 1 Store methods, and `machines.roster()` (`{machines: [{host, beats: {lane: {last_beat}}}], local}`).
- Produces:
  - `stand_down(host: str, *, by: str, note: str | None = None, now: datetime | None = None) -> dict`
  - `stand_up(host: str) -> bool`
  - `stood_down(modes: dict[str, dict], roster: dict) -> dict[str, dict]` — host → mark, for the marks still in force
  - `POST /api/machines/{host}/stand-down` with body `{note?}`, and `POST /api/machines/{host}/stand-up`

- [ ] **Step 1: Write the failing test**

```python
"""Machines stood down on purpose: the mark holds until a worker on that
machine beats again."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline import store as S
from prospector_app.backend import data, machine_mode

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _roster(**beats: str) -> dict:
    return {"local": "x", "machines": [
        {"host": h, "beats": {"verify": {"last_beat": b}}} for h, b in beats.items()]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    return st


def test_mark_holds_while_machine_is_silent(store):
    machine_mode.stand_down("lap", by="ops", note="travel", now=NOW)
    out = machine_mode.stood_down(store.load_machine_modes(), _roster(lap=_iso(30)))
    assert set(out) == {"lap"} and out["lap"]["note"] == "travel"


def test_beat_after_mark_ends_stand_down(store):
    machine_mode.stand_down("lap", by="ops", now=NOW - timedelta(hours=2))
    assert machine_mode.stood_down(store.load_machine_modes(), _roster(lap=_iso(1))) == {}


def test_host_with_no_beats_stays_stood_down(store):
    machine_mode.stand_down("gone", by="ops", now=NOW)
    assert set(machine_mode.stood_down(store.load_machine_modes(), _roster())) == {"gone"}


def test_stand_up_clears(store):
    machine_mode.stand_down("lap", by="ops", now=NOW)
    assert machine_mode.stand_up("lap") is True
    assert store.load_machine_modes() == {}


def test_endpoints(store, monkeypatch):
    from prospector_app.backend import app as app_mod
    client = TestClient(app_mod.app)
    r = client.post("/api/machines/lap/stand-down", json={"note": "off"})
    assert r.status_code == 200 and r.json()["stood_down"] is True
    assert "lap" in store.load_machine_modes()
    assert client.post("/api/machines/lap/stand-up").json() == {"cleared": True}
```

If the app's unconfigured middleware answers 409 in tests, copy the fixture an existing endpoint test in `prospector_app/backend/tests/` uses to configure the app — `grep -ln "TestClient(app" prospector_app/backend/tests | head -3` shows which — rather than inventing one.

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest prospector_app/backend/tests/test_machine_mode.py -v`
Expected: FAIL — `ImportError: cannot import name 'machine_mode'`.

- [ ] **Step 3: Implement `machine_mode.py`**

```python
"""Machines an operator stood down on purpose.

A stood-down machine is expected to be silent: its offline heartbeat and its
tripped lanes are not problems. The mark lasts until any worker on that
machine beats after it, so a machine that comes back reads as working again.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pipeline import storekit
from prospector_app.backend import data


def stand_down(host: str, *, by: str, note: str | None = None,
               now: datetime | None = None) -> dict:
    record = {"host": host, "stood_down": True,
              "at": (now or datetime.now(timezone.utc)).isoformat(),
              "by": by, "note": note}
    data.store().save_machine_mode(record)
    return record


def stand_up(host: str) -> bool:
    return data.store().clear_machine_mode(host)


def stood_down(modes: dict[str, dict], roster: dict) -> dict[str, dict]:
    beats: dict[str, list[datetime]] = {}
    for m in roster.get("machines") or []:
        stamps = [storekit.parse_ts(b.get("last_beat")) for b in (m.get("beats") or {}).values()]
        beats[str(m["host"])] = [s for s in stamps if s is not None]
    out: dict[str, dict] = {}
    for host, mark in modes.items():
        at = storekit.parse_ts(mark.get("at"))
        if not mark.get("stood_down") or at is None:
            continue
        if any(b > at for b in beats.get(host, [])):
            continue
        out[host] = mark
    return out
```

- [ ] **Step 4: Add the endpoints to `app.py`**

Add `machine_mode` to the existing `from prospector_app.backend import (...)` list (keep it alphabetical) and add these after `machines_activity`. `claims.me()` is the operator identity the claims endpoints already use.

```python
@app.post("/api/machines/{host}/stand-down")
def machines_stand_down(host: str, body: dict = Body(default={})):
    """Mark a machine off on purpose: its silence and trips stop reading as
    problems until one of its workers beats again."""
    note = body.get("note")
    return machine_mode.stand_down(host, by=str(claims.me()),
                                   note=str(note) if note else None)


@app.post("/api/machines/{host}/stand-up")
def machines_stand_up(host: str):
    return {"cleared": machine_mode.stand_up(host)}
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest prospector_app/backend/tests/test_machine_mode.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add prospector_app/backend/machine_mode.py prospector_app/backend/app.py prospector_app/backend/tests/test_machine_mode.py
git commit -m "Let an operator stand a machine down on purpose"
```

---

### Task 3: The health strip skips stood-down machines

**Files:**
- Modify: `prospector_app/backend/system_health.py`, `summarize` (line 83) and `status` (line 163)
- Test: `prospector_app/backend/tests/test_system_health.py`

**Interfaces:**
- Consumes: `machine_mode.stood_down` and `machines.roster`.
- Produces: `summarize(..., stood_down: frozenset[str] = frozenset())`.

- [ ] **Step 1: Write the failing tests** (append to `test_system_health.py`)

```python
def test_stood_down_host_drops_out_of_strip():
    out = system_health.summarize(
        [_tripped_host("lap", ["fix"])],
        [{"host": "lap", "lane": "fix", "last_beat": _iso(5), "age_seconds": 5 * 3600}],
        [("fix", "lap"), ("verify", "mac")], FRESH, NOW_TS,
        stood_down=frozenset({"lap"}))
    assert out["severity"] == "ok" and out["items"] == []
    assert out["lanes_total"] == 1 and out["lanes_down"] == 0


def test_other_hosts_still_report_when_one_is_stood_down():
    out = system_health.summarize(
        [_tripped_host("mac", ["verify"])], [],
        [("verify", "mac"), ("fix", "lap")], FRESH, NOW_TS,
        stood_down=frozenset({"lap"}))
    assert [i["kind"] for i in out["items"]] == ["lanes", "trip"]
    assert out["lanes_total"] == 1 and out["workers_stalled"] is True
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest prospector_app/backend/tests/test_system_health.py -k stood_down -v`
Expected: FAIL — `TypeError: summarize() got an unexpected keyword argument 'stood_down'`.

- [ ] **Step 3: Implement**

In `summarize`, add the keyword parameter `stood_down: frozenset[str] = frozenset()` after `now_ts`, and add one sentence to the docstring: "`stood_down` names the machines an operator stood down; they contribute nothing." Make these the first statements after `now_ts = …`:

```python
    health_hosts = [h for h in health_hosts if str(h["host"]) not in stood_down]
    offline = [o for o in offline if str(o["host"]) not in stood_down]
    worker_lanes = [(lane, host) for lane, host in worker_lanes if host not in stood_down]
```

In `status()`, compute the set before calling `summarize`, and pass it in:

```python
    from prospector_app.backend import machine_mode, machines
    stood = frozenset(machine_mode.stood_down(data.store().load_machine_modes(), machines.roster()))
    summary = summarize(health["hosts"], escalation.offline_workers(registries=regs),
                        worker_lanes, ingest_last, stood_down=stood)
```

- [ ] **Step 4: Run the file's tests**

Run: `uv run pytest prospector_app/backend/tests/test_system_health.py -v`
Expected: all pass. The existing tests are unchanged because they pass no `stood_down`.

- [ ] **Step 5: Commit**

```bash
git add prospector_app/backend/system_health.py prospector_app/backend/tests/test_system_health.py
git commit -m "Leave stood-down machines out of the health strip"
```

---

### Task 4: Worker loops record lanes and passes; the overview builds

This task has two parts that one reviewer gates together, because `build()`'s lane states depend on the row's shape.

**Files:**
- Create: `prospector_app/backend/worker_loops.py`, `prospector_app/backend/control_overview.py`
- Modify:
  - `threat_refresh.py` `_loop` (line 114)
  - `pr_watch.py` `_loop` (line 81)
  - `stale_refresh.py` `_loop`
  - `rereview_hunt.py` `_loop` (line 125)
  - `cluster_refresh.py` `_loop` (line 135)
  - `issue_fix_worker.py` `_every_ten_minutes` (line 342)
  - `app.py` (`_launch_worker_cadences`, `POST /api/setup/flags`, plus the new GET)
- Test: `prospector_app/backend/tests/test_worker_loops.py`, `prospector_app/backend/tests/test_control_overview.py`

**Interfaces — produces:**
- `worker_loops.LOOPS: dict[str, tuple[str, int]]` (loop name → (label, every_seconds))
- `worker_loops.lanes_on() -> dict[str, bool]`
- `worker_loops.ran(loop: str, now: datetime | None = None) -> None`
- `worker_loops.publish(now: datetime | None = None) -> None`
- `control_overview.LANES: tuple[str, ...] = ("security", "verify", "design", "autofix", "issue-fix", "cluster")`
- `control_overview.build(inp: Inputs, now: datetime) -> Overview`
- `control_overview.gather() -> Overview`
- `GET /api/control/overview`

Every TypedDict below is exported for Task 5 to mirror.

#### 4a. `worker_loops.py`

- [ ] **Step 1: Write the failing test** — `tests/test_worker_loops.py`

```python
"""This machine's lanes and periodic-loop passes, published to its row."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline import store as S
from prospector_app.backend import data, worker_loops

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_WORKER_ID", "mac-1")
    monkeypatch.setattr(worker_loops, "_passes", {})
    return st


def test_lanes_on_reads_the_flags(monkeypatch):
    for k in ("TRIAGE_VERIFY_WORKER", "TRIAGE_VERIFY_AUTOHUNT", "TRIAGE_FIX_WORKER",
              "TRIAGE_ISSUE_FIX_WORKER", "TRIAGE_CLUSTER_WORKER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TRIAGE_VERIFY_WORKER", "1")
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "off")
    on = worker_loops.lanes_on()
    assert on["verify"] is True
    assert on["autofix"] is False and on["cluster"] is False
    assert on["design"] is False


def test_ran_stamps_the_loop_and_keeps_others(store):
    worker_loops.ran("pr-watch", now=NOW)
    worker_loops.ran("threat-refresh", now=NOW)
    row = store.load_worker_loops()["mac-1"]
    assert row["loops"]["pr-watch"] == {"last_at": NOW.isoformat(),
                                        "every_seconds": worker_loops.LOOPS["pr-watch"][1]}
    assert set(row["loops"]) == {"pr-watch", "threat-refresh"}
    assert set(row["lanes"]) >= {"security", "verify", "autofix"}


def test_a_store_failure_never_breaks_the_loop(store, monkeypatch):
    def boom(_: dict) -> None:
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "save_worker_loops", boom)
    worker_loops.ran("pr-watch", now=NOW)
```

The first test checks `verify_worker.enabled_autohunt` and `fix_worker.enabled` against their real env reads. Before relying on that, run `sed -n 160,170p prospector_app/backend/fix_worker.py` and `sed -n 450,456p prospector_app/backend/verify_worker.py` to see which variables they read.

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest prospector_app/backend/tests/test_worker_loops.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `worker_loops.py`**

```python
"""This machine's lanes and the last pass of each periodic loop, in its own
`worker_loops:<host>` row, so any app can read which lanes a machine runs and
when its loops run next. Writing is best-effort: a failed write is printed and
the loop carries on.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone

from pipeline import settings
from prospector_app.backend import (cluster_refresh, data, fix_worker, issue_fix_worker,
                                    pr_watch, rereview_hunt, stale_refresh, threat_refresh,
                                    verify_worker)

LOOPS: dict[str, tuple[str, int]] = {
    "threat-refresh": ("Threat scan of new heads", threat_refresh.REFRESH_SECONDS),
    "pr-watch": ("PR watch", pr_watch.INTERVAL_SECONDS),
    "stale-refresh": ("Stale merge-candidate refresh", stale_refresh.REFRESH_SECONDS),
    "rereview": ("Re-review requests", rereview_hunt.REFRESH_SECONDS),
    "cluster": ("Clustering pass", cluster_refresh.REFRESH_SECONDS),
    "issue-followup": ("Issue follow-up", 10 * 60),
}

_lock = threading.Lock()
_passes: dict[str, dict] = {}


def lanes_on() -> dict[str, bool]:
    hunting = verify_worker.enabled() and verify_worker.enabled_autohunt()
    return {"security": hunting, "verify": verify_worker.enabled(),
            "design": hunting and settings.design_review_mode() != "off",
            "autofix": fix_worker.enabled(), "issue-fix": issue_fix_worker.enabled(),
            "cluster": cluster_refresh.enabled()}


def _write(now: datetime) -> None:
    record = {"host": settings.worker_id(), "at": now.isoformat(),
              "lanes": lanes_on(), "loops": dict(_passes)}
    try:
        data.store().save_worker_loops(record)
    except Exception:
        traceback.print_exc()


def ran(loop: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    with _lock:
        _passes[loop] = {"last_at": now.isoformat(), "every_seconds": LOOPS[loop][1]}
        _write(now)


def publish(now: datetime | None = None) -> None:
    with _lock:
        _write(now or datetime.now(timezone.utc))
```

If importing `worker_loops` from the loop modules creates an import cycle (each of them imports `worker_loops`, and `worker_loops` imports them for their constants), move the module-level loop-module imports inside `lanes_on`, and replace the constants in `LOOPS` with the literal seconds plus a comment naming the source constant. Run `uv run python -c "import prospector_app.backend.app"` to check.

- [ ] **Step 4: Stamp each loop**

In each `_loop`, call `worker_loops.ran("<name>")` right after the pass's `try/except`, before the wait. Import it as `from prospector_app.backend import worker_loops`.

  - `threat_refresh._loop`: after the `scan_new_heads()` try block, `worker_loops.ran("threat-refresh")`.
  - `pr_watch._loop`: after the `watch_once()` try block, `worker_loops.ran("pr-watch")`.
  - `stale_refresh._loop`: after the `refresh_stale()` try block, `worker_loops.ran("stale-refresh")`.
  - `rereview_hunt._loop`: after the `request_rereviews()` try block, `worker_loops.ran("rereview")`.
  - `cluster_refresh._loop`: after the `pass_once()` try block, `worker_loops.ran("cluster")`.
  - `issue_fix_worker._every_ten_minutes`: wrap the `for` loop in `try: … finally: worker_loops.ran("issue-followup")`, so the early `return` on an agent outage still stamps.

- [ ] **Step 5: Publish at startup and when flags change**

At the end of `_launch_worker_cadences` in `app.py` (after `cluster_refresh.startup()`), add `worker_loops.publish()`. In the `POST /api/setup/flags` handler, call `worker_loops.publish()` after `worker_control.apply()` returns. Read the handler first with `grep -n "/api/setup/flags" -A20 prospector_app/backend/app.py`.

- [ ] **Step 6: Run the tests and an import check**

Run: `uv run pytest prospector_app/backend/tests/test_worker_loops.py -v && uv run python -c "import prospector_app.backend.app"`
Expected: 3 passed, and the import succeeds.

- [ ] **Step 7: Commit**

```bash
git add prospector_app/backend/worker_loops.py prospector_app/backend/tests/test_worker_loops.py prospector_app/backend/{threat_refresh,pr_watch,stale_refresh,rereview_hunt,cluster_refresh,issue_fix_worker,app}.py
git commit -m "Record each machine's lanes and periodic-loop passes"
```

#### 4b. `control_overview.build`

- [ ] **Step 8: Write the failing tests** — `tests/test_control_overview.py`

```python
"""The Health & queues overview: machines, lanes, loops, needs, timeline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prospector_app.backend import control_overview as co

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ALL_ON = {lane: True for lane in co.LANES}


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _machine(host: str, beat_min: float, **beats: dict) -> dict:
    return {"host": host, "online": beat_min < 2, "ai_account": {"key": "acct", "label": "x"},
            "beats": beats or {"verify": {"last_beat": _iso(beat_min), "online": beat_min < 2,
                                          "current_pr": None, "current_issue": None}}}


def _inputs(**over) -> co.Inputs:
    base: co.Inputs = {
        "local": "studio",
        "roster": {"local": "studio", "machines": [_machine("studio", 0.5)]},
        "stood_down": {}, "loops": {"studio": {"lanes": ALL_ON, "loops": {}}},
        "tripped": {}, "pins": [], "ai_paused": {},
        "verify_queue": [], "fix_queue": [], "issue_queue": [], "hunter_now": {},
        "pools": {"security": 0, "verify": 0, "design": 0},
        "failed_runs": [], "rows": []}
    base.update(over)  # type: ignore[typeddict-item]
    return base


def _cell(out: co.Overview, lane: str, host: str) -> str:
    row = next(r for r in out["lanes"] if r["lane"] == lane)
    return next(c["state"] for c in row["cells"] if c["host"] == host)


def test_flag_off_lane_reads_off_and_is_not_a_need():
    out = co.build(_inputs(loops={"studio": {"lanes": {**ALL_ON, "cluster": False}, "loops": {}}}), NOW)
    assert _cell(out, "cluster", "studio") == "off"
    assert out["needs"] == []


def test_tripped_lane_is_a_need_unless_stood_down():
    tripped = {"studio": {"design": {"at": _iso(120), "kind": "coverage", "reason": "r"}}}
    out = co.build(_inputs(tripped=tripped), NOW)
    assert _cell(out, "design", "studio") == "tripped"
    assert [n["key"] for n in out["needs"]] == ["trip:studio:design"]
    out = co.build(_inputs(tripped=tripped, stood_down={"studio": {"at": _iso(1)}}), NOW)
    assert _cell(out, "design", "studio") == "stood-down" and out["needs"] == []


def test_offline_machine_is_a_need_unless_stood_down():
    roster = {"local": "studio", "machines": [_machine("studio", 0.5), _machine("lap", 300)]}
    loops = {"studio": {"lanes": ALL_ON, "loops": {}}, "lap": {"lanes": ALL_ON, "loops": {}}}
    out = co.build(_inputs(roster=roster, loops=loops), NOW)
    assert "offline:lap" in [n["key"] for n in out["needs"]]
    assert next(m for m in out["machines"] if m["host"] == "lap")["state"] == "offline"
    out = co.build(_inputs(roster=roster, loops=loops, stood_down={"lap": {"at": _iso(400)}}), NOW)
    assert "offline:lap" not in [n["key"] for n in out["needs"]]
    lap = next(m for m in out["machines"] if m["host"] == "lap")
    assert lap["state"] == "off" and lap["stood_down"] is True


def test_lanes_fall_back_to_beats_without_loops_row():
    roster = {"local": "studio", "machines": [_machine(
        "old", 0.5, fix={"last_beat": _iso(0.5), "online": True,
                         "current_pr": None, "current_issue": None})]}
    out = co.build(_inputs(roster=roster, loops={}), NOW)
    assert _cell(out, "autofix", "old") == "on"
    assert _cell(out, "verify", "old") == "off"


def test_ai_paused_lane_state():
    out = co.build(_inputs(ai_paused={"acct": {"reason": "weekly", "retry_at": _iso(-60)}}), NOW)
    assert _cell(out, "autofix", "studio") == "ai-paused"
    assert out["ai_paused"] is not None and out["ai_paused"]["reason"] == "weekly"


def test_loop_next_run_is_last_plus_interval():
    loops = {"studio": {"lanes": ALL_ON, "loops": {
        "pr-watch": {"last_at": _iso(9), "every_seconds": 900}}}}
    [loop] = co.build(_inputs(loops=loops), NOW)["loops"]
    assert loop["next_at"] == _iso(9 - 15)


def test_loops_on_offline_host_have_no_next():
    roster = {"local": "studio", "machines": [_machine("studio", 300)]}
    loops = {"studio": {"lanes": ALL_ON, "loops": {
        "pr-watch": {"last_at": _iso(9), "every_seconds": 900}}}}
    [loop] = co.build(_inputs(roster=roster, loops=loops), NOW)["loops"]
    assert loop["next_at"] is None


def test_queue_head_and_now():
    vq = [{"pr": 7, "title": "a", "status": "running", "source": "auto", "step": "green",
           "queued_at": _iso(10), "started_at": _iso(3), "host": "studio"},
          {"pr": 8, "title": "b", "status": "queued", "source": "operator", "step": None,
           "queued_at": _iso(5), "started_at": None, "host": None}]
    out = co.build(_inputs(verify_queue=vq, pools={"security": 4, "verify": 2, "design": 0}), NOW)
    row = next(r for r in out["lanes"] if r["lane"] == "verify")
    assert [n["pr"] for n in row["now"]] == [7]
    assert [q["pr"] for q in row["queue"]] == [8]
    sec = next(r for r in out["lanes"] if r["lane"] == "security")
    assert sec["pool"] == 4 and sec["queue"] == []


def test_parked_changes_are_one_need():
    fq = [{"pr": p, "title": None, "status": "awaiting-review", "action": "fix", "source": "auto",
           "step": None, "queued_at": _iso(60 * p), "started_at": None, "host": None}
          for p in (1, 2)]
    [need] = co.build(_inputs(fix_queue=fq), NOW)["needs"]
    assert need["key"] == "parked" and need["count"] == 2 and need["at"] == _iso(120)


def test_timeline_lists_endings_and_collapses_background():
    rows = [("pr", {"phase": "security:review-one", "pr": 5, "host": "studio",
                    "finished": _iso(20), "stats": {"verdict": "GREEN"}}),
            ("pr", {"phase": "fix:single", "pr": 6, "host": "studio", "finished": _iso(30),
                    "stats": {"status": "failed", "kind": "sandbox"}}),
            ("pr", {"phase": "ingest:watch", "host": "studio", "finished": _iso(40)}),
            ("pr", {"phase": "ingest:watch", "host": "studio", "finished": _iso(45)})]
    past = [t for t in co.build(_inputs(rows=rows), NOW)["timeline"] if t["when"] == "past"]
    assert [(t["lane"], t["pr"], t["bad"]) for t in past[:2]] == [
        ("security", 5, False), ("autofix", 6, True)]
    watch = [t for t in past if t["lane"] == "background"]
    assert len(watch) == 1 and watch[0]["text"] == "PR watch ×2"
    row = next(r for r in co.build(_inputs(rows=rows), NOW)["lanes"] if r["lane"] == "autofix")
    assert row["ended_24h"] == 1 and row["faults_24h"] == 1
```

The two PR-watch rows are 40 and 45 minutes old with NOW at 12:00, so both fall in the 11:00 hour bucket.

- [ ] **Step 9: Run them to see them fail**

Run: `uv run pytest prospector_app/backend/tests/test_control_overview.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 10: Implement `control_overview.py`**

```python
"""The Health & queues overview: one read of which machines and lanes are on,
what runs now, what runs next, what ran, and what needs a person.

`build` is pure over plain data; `gather` reads the live inputs. A lane's
state on a machine is the first that holds of: stood down, offline, off (its
flag), tripped, AI paused, on.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict

from pipeline import capacity, gates, storekit
from prospector_app.backend import machine_activity

LANES: tuple[str, ...] = ("security", "verify", "design", "autofix", "issue-fix", "cluster")
LABELS: dict[str, str] = {"security": "Security", "verify": "Verify", "design": "Design",
                          "autofix": "Autofix", "issue-fix": "Issue fix", "cluster": "Clustering"}
# worker_health's name for each lane.
HEALTH_LANE: dict[str, str] = {"security": "security", "verify": "verify", "design": "design",
                               "autofix": "fix", "issue-fix": "issue-fix", "cluster": "cluster"}
# The heartbeat each lane runs under, for a machine with no worker_loops row.
BEAT_LANE: dict[str, str] = {"security": "verify", "verify": "verify", "design": "verify",
                             "autofix": "fix", "issue-fix": "issue-fix", "cluster": "verify"}
# The ledger phase each lane books an ending under.
LANE_PHASES: dict[str, str] = {"security:review-one": "security", "verify:single": "verify",
                               "design-review:run": "design", "fix:single": "autofix",
                               "issue-fix:run": "issue-fix", "cluster:summaries": "cluster",
                               "cluster:assign": "cluster", "analyze:commit": "cluster"}
PAST_LIMIT = 60
WINDOW = timedelta(hours=24)

CellState = Literal["on", "off", "tripped", "ai-paused", "stood-down", "offline"]
MachineState = Literal["working", "idle", "off", "offline"]
NeedKind = Literal["trip", "offline", "pins", "failed-runs", "parked"]


class Cell(TypedDict):
    host: str
    state: CellState


class NowItem(TypedDict):
    host: str | None
    pr: int | None
    issue: int | None
    title: str | None
    step: str | None
    started_at: str | None


class QueueItem(TypedDict):
    pr: int | None
    issue: int | None
    title: str | None
    status: str
    source: str | None
    queued_at: str | None


class LaneRow(TypedDict):
    lane: str
    label: str
    cells: list[Cell]
    now: list[NowItem]
    queue: list[QueueItem]
    parked: int
    pool: int | None
    ended_24h: int
    faults_24h: int


class LoopRow(TypedDict):
    loop: str
    label: str
    host: str
    last_at: str | None
    next_at: str | None


class Machine(TypedDict):
    host: str
    local: bool
    state: MachineState
    last_beat: str | None
    lanes_on: int
    stood_down: bool
    stood_down_note: str | None


class Need(TypedDict):
    key: str
    kind: NeedKind
    title: str
    at: str | None
    host: str | None
    lane: str | None
    detail: str | None
    count: int
    groups: list[dict]


class TimelineItem(TypedDict):
    when: Literal["past", "now", "upcoming"]
    at: str
    lane: str
    host: str | None
    pr: int | None
    issue: int | None
    text: str
    bad: bool


class AiPaused(TypedDict):
    reason: str
    retry_at: str | None


class Overview(TypedDict):
    local: str
    machines: list[Machine]
    lanes: list[LaneRow]
    loops: list[LoopRow]
    needs: list[Need]
    timeline: list[TimelineItem]
    ai_paused: AiPaused | None


class Inputs(TypedDict):
    local: str
    roster: dict                              # machines.roster()
    stood_down: dict[str, dict]               # machine_mode.stood_down(...)
    loops: dict[str, dict]                    # Store.load_worker_loops()
    tripped: dict[str, dict[str, dict]]       # host → health lane → trip stamp {at, kind, reason}
    pins: list[dict]                          # autohunt_view.base_health()["hosts"] in trouble
    ai_paused: dict[str, AiPaused]            # account key → decision, paused accounts only
    verify_queue: list[dict]                  # verify_queue.queue_entries()
    fix_queue: list[dict]                     # fix_queue.queue_entries()
    issue_queue: list[dict]                   # {issue, title, status, source, queued_at, started_at, host, step}
    hunter_now: dict[str, list[NowItem]]      # "security"/"design" → in-flight hunter runs
    pools: dict[str, int]                     # security / verify / design pool sizes
    failed_runs: list[dict]                   # [{lane, reason, prs}]
    rows: list[tuple[str, dict]]              # (ledger, record) in the past 24h


def _beat_lanes(m: dict) -> dict[str, bool]:
    beats = m.get("beats") or {}
    return {lane: BEAT_LANE[lane] in beats and lane != "cluster" for lane in LANES}


def _cell(lane: str, host: str, m: dict, inp: Inputs) -> CellState:
    if host in inp["stood_down"]:
        return "stood-down"
    if not m.get("online"):
        return "offline"
    row = inp["loops"].get(host)
    on = (row or {}).get("lanes") if row else _beat_lanes(m)
    if not (on or {}).get(lane):
        return "off"
    if HEALTH_LANE[lane] in inp["tripped"].get(host, {}):
        return "tripped"
    acct = (m.get("ai_account") or {}).get("key")
    if acct and acct in inp["ai_paused"]:
        return "ai-paused"
    return "on"


def _outcome_bad(lane: str, stats: dict) -> bool:
    if lane == "verify":
        return stats.get("status") == "error" and stats.get("error_kind") in gates.VERIFY_REQUEST_FAULT
    if lane == "autofix":
        return stats.get("status") == "failed" and stats.get("kind") != capacity.PAUSED_KIND
    if lane == "issue-fix":
        return stats.get("ending") == "failed"
    return stats.get("status") == "failed"


def _outcome_text(lane: str, stats: dict) -> str:
    if lane == "security" or lane == "verify" or lane == "autofix":
        return machine_activity.outcome(lane, stats)
    return str(stats.get("ending") or stats.get("status") or "done")


def _need_rows(inp: Inputs, machines: list[Machine], fq: list[dict]) -> list[Need]:
    def need(key: str, kind: NeedKind, title: str, *, at: str | None = None,
             host: str | None = None, lane: str | None = None, detail: str | None = None,
             count: int = 1, groups: list[dict] | None = None) -> Need:
        return {"key": key, "kind": kind, "title": title, "at": at, "host": host,
                "lane": lane, "detail": detail, "count": count, "groups": groups or []}

    out: list[Need] = []
    online = {m["host"] for m in machines if m["state"] in ("working", "idle")}
    for host, lanes in sorted(inp["tripped"].items()):
        if host not in online:
            continue
        for lane, t in sorted(lanes.items()):
            label = next((LABELS[k] for k, v in HEALTH_LANE.items() if v == lane), lane)
            out.append(need(f"trip:{host}:{lane}", "trip",
                            f"{label} lane tripped on {host}: {t.get('kind') or 'unknown'}",
                            at=t.get("at"), host=host, lane=lane, detail=t.get("reason")))
    for m in machines:
        if m["state"] == "offline":
            out.append(need(f"offline:{m['host']}", "offline", f"{m['host']} worker offline",
                            at=m["last_beat"], host=m["host"]))
    pins = [p for p in inp["pins"] if p.get("host") not in inp["stood_down"]]
    if pins:
        out.append(need("pins", "pins", f"Base pin refresh failing on {len(pins)} "
                        f"machine{'s' if len(pins) != 1 else ''}", count=len(pins),
                        detail="\n".join(f"{p['host']}: {p.get('refresh_error') or 'stale'}"
                                         for p in pins)))
    runs = sum(len(g["prs"]) for g in inp["failed_runs"])
    if runs:
        out.append(need("failed-runs", "failed-runs", f"{runs} failed hunter "
                        f"run{'s' if runs != 1 else ''}", count=runs, groups=inp["failed_runs"]))
    parked = [e for e in fq if e.get("status") == "awaiting-review"]
    if parked:
        oldest = min((e.get("queued_at") or "" for e in parked), default="") or None
        out.append(need("parked", "parked", f"{len(parked)} parked change"
                        f"{'s' if len(parked) != 1 else ''} waiting for your push",
                        at=oldest, count=len(parked), lane="autofix"))
    return out


def build(inp: Inputs, now: datetime) -> Overview:
    cutoff = now - WINDOW
    roster = inp["roster"].get("machines") or []
    machines: list[Machine] = []
    lane_cells: dict[str, list[Cell]] = {lane: [] for lane in LANES}
    for m in roster:
        host = str(m["host"])
        cells = {lane: _cell(lane, host, m, inp) for lane in LANES}
        for lane, state in cells.items():
            lane_cells[lane].append({"host": host, "state": state})
        beats = [b.get("last_beat") for b in (m.get("beats") or {}).values() if b.get("last_beat")]
        busy = any(b.get("current_pr") is not None or b.get("current_issue") is not None
                   for b in (m.get("beats") or {}).values())
        n_on = sum(1 for s in cells.values() if s in ("on", "tripped", "ai-paused"))
        state: MachineState = ("off" if host in inp["stood_down"] or (m.get("online") and n_on == 0)
                               else "offline" if not m.get("online")
                               else "working" if busy else "idle")
        machines.append({"host": host, "local": host == inp["local"], "state": state,
                         "last_beat": max(beats, default=None), "lanes_on": n_on,
                         "stood_down": host in inp["stood_down"],
                         "stood_down_note": (inp["stood_down"].get(host) or {}).get("note")})

    vq, fq, iq = inp["verify_queue"], inp["fix_queue"], inp["issue_queue"]

    def now_of(entries: list[dict], running: tuple[str, ...], key: str) -> list[NowItem]:
        return [{"host": e.get("host"), "pr": e.get("pr") if key == "pr" else None,
                 "issue": e.get("issue") if key == "issue" else None, "title": e.get("title"),
                 "step": e.get("step"), "started_at": e.get("started_at")}
                for e in entries if e.get("status") in running]

    def queue_of(entries: list[dict], waiting: tuple[str, ...], key: str) -> list[QueueItem]:
        return [{"pr": e.get("pr") if key == "pr" else None,
                 "issue": e.get("issue") if key == "issue" else None, "title": e.get("title"),
                 "status": str(e.get("status")), "source": e.get("source"),
                 "queued_at": e.get("queued_at")}
                for e in entries if e.get("status") in waiting]

    nows = {"verify": now_of(vq, ("running",), "pr"),
            "autofix": now_of(fq, ("running", "pushing"), "pr"),
            "issue-fix": now_of(iq, ("running",), "issue"),
            "security": inp["hunter_now"].get("security", []),
            "design": inp["hunter_now"].get("design", []), "cluster": []}
    queues = {"verify": queue_of(vq, ("queued", "waiting-for-base"), "pr"),
              "autofix": queue_of(fq, ("queued", "approved"), "pr"),
              "issue-fix": queue_of(iq, ("queued",), "issue")}

    ended: Counter[str] = Counter()
    faults: Counter[str] = Counter()
    past: list[TimelineItem] = []
    background: Counter[tuple[str, str, str | None]] = Counter()
    for _ledger, raw in inp["rows"]:
        when = storekit.parse_ts(raw.get("finished") or raw.get("started"))
        phase = raw.get("phase")
        if when is None or when < cutoff or when > now or not isinstance(phase, str):
            continue
        host = machine_activity.row_host(raw)
        stats = s if isinstance(s := raw.get("stats"), dict) else {}
        if phase in LANE_PHASES:
            lane = LANE_PHASES[phase]
            bad = _outcome_bad(lane, stats)
            ended[lane] += 1
            faults[lane] += bad
            pr = raw.get("pr") if isinstance(raw.get("pr"), int) else None
            issue = raw.get("issue") if isinstance(raw.get("issue"), int) else None
            text = _outcome_text(lane, stats)
            past.append({"when": "past", "at": when.isoformat(), "lane": lane, "host": host,
                         "pr": pr, "issue": issue, "bad": bad,
                         "text": f"#{pr or issue} → {text}" if (pr or issue) else text})
        elif phase in machine_activity.BACKGROUND:
            hour = when.replace(minute=0, second=0, microsecond=0).isoformat()
            background[(machine_activity.BACKGROUND[phase][0], hour, host)] += 1
    for (label, hour, host), n in background.items():
        past.append({"when": "past", "at": hour, "lane": "background", "host": host,
                     "pr": None, "issue": None, "bad": False,
                     "text": label if n == 1 else f"{label} ×{n}"})
    past.sort(key=lambda t: t["at"], reverse=True)

    online = {m["host"] for m in machines if m["state"] in ("working", "idle")}
    loops: list[LoopRow] = []
    from prospector_app.backend import worker_loops
    for host, row in sorted(inp["loops"].items()):
        for name, p in sorted((row.get("loops") or {}).items()):
            last = storekit.parse_ts(p.get("last_at"))
            nxt = (last + timedelta(seconds=int(p.get("every_seconds") or 0))
                   if last and host in online else None)
            loops.append({"loop": name, "label": worker_loops.LOOPS.get(name, (name, 0))[0],
                          "host": host, "last_at": p.get("last_at"),
                          "next_at": nxt.isoformat() if nxt else None})

    upcoming: list[TimelineItem] = [
        {"when": "upcoming", "at": lp["next_at"], "lane": "background", "host": lp["host"],
         "pr": None, "issue": None, "bad": False, "text": lp["label"]}
        for lp in loops if lp["next_at"] and lp["next_at"] <= (now + timedelta(hours=1)).isoformat()]
    for lane, q in queues.items():
        if q:
            upcoming.append({"when": "upcoming", "at": now.isoformat(), "lane": lane, "host": None,
                             "pr": q[0]["pr"], "issue": q[0]["issue"], "bad": False,
                             "text": f"next: #{q[0]['pr'] or q[0]['issue']}"})
    upcoming.sort(key=lambda t: t["at"])
    current: list[TimelineItem] = [
        {"when": "now", "at": n["started_at"] or now.isoformat(), "lane": lane, "host": n["host"],
         "pr": n["pr"], "issue": n["issue"], "bad": False,
         "text": f"#{n['pr'] or n['issue']}" + (f" · {n['step']}" if n["step"] else "")}
        for lane, items in nows.items() for n in items]

    lanes: list[LaneRow] = [{
        "lane": lane, "label": LABELS[lane], "cells": lane_cells[lane], "now": nows[lane],
        "queue": queues.get(lane, []),
        "parked": sum(1 for e in fq if e.get("status") == "awaiting-review") if lane == "autofix" else 0,
        "pool": inp["pools"].get(lane), "ended_24h": ended[lane], "faults_24h": faults[lane],
    } for lane in LANES]

    paused = next(iter(inp["ai_paused"].values()), None)
    return {"local": inp["local"], "machines": machines, "lanes": lanes, "loops": loops,
            "needs": _need_rows(inp, machines, fq),
            "timeline": upcoming + current + past[:PAST_LIMIT], "ai_paused": paused}
```

Notes for the implementer:
- Rename `machine_activity._outcome` to `outcome` and update its one caller in `summarize`. It is the one wording for those lanes' endings, and it now has a second reader. The `==` chain in `_outcome_text` narrows `lane` to the `machine_activity.Lane` literal for pyright.
- If `"design-review:run"` endings carry their outcome under another key, read one live row (`grep -rn "design-review:run" prospector_app/backend pipeline | head`) and adjust `_outcome_text`.
- `_beat_lanes` reports cluster as off for a machine with no `worker_loops` row, because cluster has no heartbeat of its own.

- [ ] **Step 11: Run the tests**

Run: `uv run pytest prospector_app/backend/tests/test_control_overview.py -v`
Expected: 11 passed. Fix the code, never the expectations, unless an expectation contradicts the spec.

- [ ] **Step 12: Add `gather()` and the endpoint**

Append to `control_overview.py`:

```python
def _issue_queue() -> list[dict]:
    from issue_triage import fix_review
    from issue_triage.issue_store import IssueStore
    store = IssueStore()
    issues = store.issues_matching(("fix_request", "status"), ["queued", "running"])
    order = {n: i for i, n in enumerate(fix_review.queued_issues(issues))}
    out = []
    for n, issue in issues.items():
        req = issue.fix_request or {}
        out.append({"issue": n, "title": issue.title, "status": req.get("status"),
                    "source": req.get("source"), "queued_at": req.get("queued_at"),
                    "started_at": req.get("started_at"), "host": req.get("host"),
                    "step": req.get("step")})
    return sorted(out, key=lambda e: (e["status"] != "running", order.get(e["issue"], 1 << 30)))


def _hunter_now(roster: dict, verify_running: set[int]) -> dict[str, list[NowItem]]:
    from prospector_app.backend import data
    prs = data.prs()
    out: dict[str, list[NowItem]] = {"security": [], "design": []}
    for m in roster.get("machines") or []:
        beat = (m.get("beats") or {}).get("verify") or {}
        n = beat.get("current_pr")
        if not beat.get("online") or n is None or n in verify_running:
            continue
        pr = prs.get(n)
        claim = (pr.raw.get("design_run") or {}) if pr else {}
        lane = "design" if claim.get("host") == m["host"] else "security"
        out[lane].append({"host": m["host"], "pr": n, "issue": None,
                          "title": pr.title if pr else None, "step": None,
                          "started_at": claim.get("claimed_at") if lane == "design" else None})
    return out


def gather() -> Overview:
    from pipeline import design_review, settings
    from prospector_app.backend import (alert_data, autohunt_view, capacity_view, data, escalation,
                                        fix_queue, issues, machine_mode, machines, verify_queue)
    now = datetime.now(timezone.utc)
    st = data.store()
    roster = machines.roster()
    stood = machine_mode.stood_down(st.load_machine_modes(), roster)
    tripped = {str(h["host"]): {lane: ((h.get("lanes") or {}).get(lane) or {}).get("tripped") or {}
                                for lane in h["tripped"]}
               for h in escalation.health_status()["hosts"] if h.get("tripped")}
    hunt = autohunt_view.status()
    pins = [p for p in hunt["base"]["hosts"] if p["refresh_failures"] > 0 or p["stale"]]
    paused = {a["key"]: {"reason": a["decision"]["reason"], "retry_at": a["decision"]["retry_at"]}
              for a in capacity_view.accounts() if a["machines"] and not a["decision"]["allowed"]}
    vq = verify_queue.queue_entries()
    groups: dict[tuple[str, str], list[int]] = {}
    for n in hunt["security_failed"]:
        reason = (hunt["security_failed_reasons"].get(str(n)) or "run failed").strip()
        groups.setdefault(("security", reason), []).append(n)
    for f in hunt["verify_failed"]:
        groups.setdefault(("verify", (f.get("error_kind") or "run failed").strip()), []).append(f["pr"])
    since = (now - WINDOW).isoformat()
    rows: list[tuple[str, dict]] = []
    for ledger, records in (("pr", data.runs(since=since)), ("issue", issues.cached_runs()),
                            ("alert", alert_data.runs())):
        rows.extend((ledger, r.raw) for r in records if isinstance(r, storekit.PhaseRun))
    open_prs = [pr for pr in data.prs().values() if pr.state == "open"]
    inp: Inputs = {
        "local": settings.worker_id(), "roster": roster, "stood_down": stood,
        "loops": st.load_worker_loops(), "tripped": tripped, "pins": pins, "ai_paused": paused,
        "verify_queue": vq, "fix_queue": fix_queue.queue_entries(), "issue_queue": _issue_queue(),
        "hunter_now": _hunter_now(roster, {e["pr"] for e in vq if e["status"] == "running"}),
        "pools": {"security": hunt["security_pool"], "verify": hunt["verify_pool"],
                  "design": sum(1 for pr in open_prs if design_review.huntable(pr))},
        "failed_runs": [{"lane": lane, "reason": reason, "prs": prs}
                        for (lane, reason), prs in sorted(groups.items(), key=lambda kv: -len(kv[1]))],
        "rows": rows}
    return build(inp, now)
```

In `app.py`, add `control_overview` to the import list and add:

```python
@app.get("/api/control/overview")
def control_overview_get() -> control_overview.Overview:
    """Health & queues in one read: machines, lanes, loops, needs, timeline."""
    return control_overview.gather()
```

Before running anything, confirm two names:
- `escalation.health_status()["hosts"][i]["lanes"][lane]["tripped"]` is the trip stamp dict. `system_health.summarize` reads it that way (`entry.get("tripped")`).
- The design claim's timestamp key: `grep -n "design_run" pipeline/store.py | head`. Use the real key in place of `claimed_at`.

- [ ] **Step 13: Smoke-test `gather` on a scratch store**

Run: `uv run pytest prospector_app/backend/tests -q -x`
Expected: the whole backend suite passes.

Then run `TRIAGE_STORE_URL= uv run python -c "from prospector_app.backend import control_overview as c; import json; print(json.dumps(c.gather())[:400])"` with the env configured for the local SQLite default, never the shared store. Expected: a JSON object with `machines`, `lanes`, `timeline` and no traceback.

- [ ] **Step 14: Gates and commit**

Run: `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness && uv run ruff check .`
Expected: 0 errors, 0 findings.

```bash
git add prospector_app/backend/control_overview.py prospector_app/backend/machine_activity.py prospector_app/backend/app.py prospector_app/backend/tests/test_control_overview.py
git commit -m "Serve the Health & queues overview in one read"
```

---

### Task 5: Frontend API types and wording helpers

**Files:**
- Modify: `prospector_app/frontend/src/api.ts` (types near `ActivityView` around line 205; calls near `machinesActivity` around line 2337)
- Create: `src/views/control/overviewText.ts`, `src/views/control/overviewText.test.ts`

**Interfaces — produces:** the TS types `ControlOverview`, `OverviewLane`, `OverviewCell`, `OverviewNeed`, `OverviewTimelineItem`, `OverviewMachine`, `OverviewQueueItem`, `OverviewNowItem`, `OverviewLoop`; the calls `api.controlOverview()`, `api.standDown(host, note?)`, `api.standUp(host)`; and from `overviewText.ts`: `cellLabel`, `nextText`, `statusLine`.

- [ ] **Step 1: Add the types and calls to `api.ts`**

```ts
export type OverviewCellState = "on" | "off" | "tripped" | "ai-paused" | "stood-down" | "offline";
export interface OverviewCell { host: string; state: OverviewCellState }
export interface OverviewNowItem { host: string | null; pr: number | null; issue: number | null; title: string | null; step: string | null; started_at: string | null }
export interface OverviewQueueItem { pr: number | null; issue: number | null; title: string | null; status: string; source: string | null; queued_at: string | null }
export interface OverviewLane {
  lane: string; label: string; cells: OverviewCell[]; now: OverviewNowItem[]; queue: OverviewQueueItem[];
  parked: number; pool: number | null; ended_24h: number; faults_24h: number;
}
export interface OverviewLoop { loop: string; label: string; host: string; last_at: string | null; next_at: string | null }
export interface OverviewMachine { host: string; local: boolean; state: "working" | "idle" | "off" | "offline"; last_beat: string | null; lanes_on: number; stood_down: boolean; stood_down_note: string | null }
export interface OverviewNeed {
  key: string; kind: "trip" | "offline" | "pins" | "failed-runs" | "parked"; title: string; at: string | null;
  host: string | null; lane: string | null; detail: string | null; count: number;
  groups: { lane: "security" | "verify"; reason: string; prs: number[] }[];
}
export interface OverviewTimelineItem { when: "past" | "now" | "upcoming"; at: string; lane: string; host: string | null; pr: number | null; issue: number | null; text: string; bad: boolean }
export interface ControlOverview {
  local: string; machines: OverviewMachine[]; lanes: OverviewLane[]; loops: OverviewLoop[];
  needs: OverviewNeed[]; timeline: OverviewTimelineItem[]; ai_paused: { reason: string; retry_at: string | null } | null;
}
```

Add these calls to the `api` object, next to `machinesActivity`:

```ts
  controlOverview: () => get<ControlOverview>("/api/control/overview"),
  standDown: (host: string, note?: string) =>
    postJson<{ stood_down: boolean }>(`/api/machines/${encodeURIComponent(host)}/stand-down`, { note: note ?? null }),
  standUp: (host: string) =>
    postJson<{ cleared: boolean }>(`/api/machines/${encodeURIComponent(host)}/stand-up`, {}),
```

- [ ] **Step 2: Write the failing test** — `overviewText.test.ts`

```ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { cellLabel, nextText, statusLine } from "./overviewText.ts";
import type { ControlOverview, OverviewLane } from "../../api.ts";

const lane = (over: Partial<OverviewLane>): OverviewLane => ({
  lane: "verify", label: "Verify", cells: [], now: [], queue: [], parked: 0, pool: null,
  ended_24h: 0, faults_24h: 0, ...over });

test("cell labels read as words", () => {
  assert.equal(cellLabel("ai-paused"), "AI paused");
  assert.equal(cellLabel("stood-down"), "stood down");
  assert.equal(cellLabel("on"), "on");
});

test("next prefers the queue head, then the pool, then nothing", () => {
  const q = { pr: 8, issue: null, title: "b", status: "queued", source: "operator", queued_at: null };
  assert.equal(nextText(lane({ queue: [q, q] }), null), "#8 · 2 queued");
  assert.equal(nextText(lane({ pool: 4 }), null), "4 in hunter pool");
  assert.equal(nextText(lane({ pool: 0 }), null), "nothing waiting");
  assert.equal(nextText(lane({ cells: [{ host: "a", state: "ai-paused" }] }), "2026-10-14T23:00:00Z"),
    "after AI resumes");
});

test("status line names working, off and offline machines and the AI hold", () => {
  const o = {
    machines: [
      { host: "studio", local: true, state: "working", last_beat: null, lanes_on: 2, stood_down_note: null },
      { host: "lap", local: false, state: "off", last_beat: null, lanes_on: 0, stood_down_note: null },
    ],
    ai_paused: { reason: "weekly limit", retry_at: null },
  } as unknown as ControlOverview;
  assert.equal(statusLine(o), "studio working on 2 lanes · lap off · background AI paused: weekly limit");
});
```

- [ ] **Step 3: Run it to see it fail**

Run (from `prospector_app/frontend`): `pnpm test`
Expected: FAIL — cannot find module `./overviewText.ts`.

- [ ] **Step 4: Implement `overviewText.ts`**

```ts
/** Wording for the Health & queues overview: lane states, the Next cell, and
 *  the one-line status of every machine. */
import type { ControlOverview, OverviewCellState, OverviewLane } from "../../api";

const CELL: Record<OverviewCellState, string> = {
  on: "on", off: "off", tripped: "tripped", "ai-paused": "AI paused",
  "stood-down": "stood down", offline: "offline",
};

export function cellLabel(state: OverviewCellState): string {
  return CELL[state];
}

export function nextText(lane: OverviewLane, aiRetryAt: string | null): string {
  const head = lane.queue[0];
  if (head) return `#${head.pr ?? head.issue} · ${lane.queue.length} queued`;
  if (lane.cells.length > 0 && lane.cells.every((c) => c.state === "ai-paused")) {
    return aiRetryAt ? "after AI resumes" : "waiting for AI";
  }
  if (lane.pool != null && lane.pool > 0) return `${lane.pool} in hunter pool`;
  return lane.pool != null || lane.cells.some((c) => c.state === "on") ? "nothing waiting" : "—";
}

export function statusLine(o: ControlOverview): string {
  const parts = o.machines.map((m) =>
    m.state === "working" ? `${m.host} working on ${m.lanes_on} lane${m.lanes_on === 1 ? "" : "s"}`
      : m.state === "idle" ? `${m.host} idle`
        : m.state === "off" ? `${m.host} off`
          : `${m.host} offline`);
  if (o.ai_paused) parts.push(`background AI paused: ${o.ai_paused.reason}`);
  return parts.join(" · ");
}
```

The `"after AI resumes"` test sets every cell to `ai-paused` (one cell), so it takes the AI branch before the pool check. Keep that order.

- [ ] **Step 5: Run the tests**

Run: `pnpm test`
Expected: all pass, including the new file.

- [ ] **Step 6: Commit**

```bash
git add prospector_app/frontend/src/api.ts prospector_app/frontend/src/views/control/overviewText.ts prospector_app/frontend/src/views/control/overviewText.test.ts
git commit -m "Type the control overview and word its states"
```

---

### Task 6: Mute, Attention, Lanes, Timeline, StatusLine components

**Files:**
- Create: `src/views/control/mute.ts`, `mute.test.ts`, `StatusLine.tsx`, `Attention.tsx`, `Lanes.tsx`, `Timeline.tsx`
- Modify: `src/views/control/Queues.tsx` — export `FixTable` and `VerifyTable`; delete the `Queues` function and `plural`
- Modify: `src/styles.css`

**Interfaces:**
- Consumes: Task 5's types; `ago`, `elapsed` and `fmt` from `format.ts`; `PRLink`; `FlyoutIssueLink`; `api.workerHealthResume(host, lane)`.
- Produces:
  - `readMutes(storage?) -> Record<string, number>`
  - `mute(key, now, storage?) -> void`
  - `isMuted(key, mutes, now) -> boolean`
  - `<StatusLine overview onChanged/>`, `<Attention overview onChanged onReviewParked/>`, `<Lanes overview open onToggle verifyQueue fixQueue hunt onFixChanged/>`, `<Timeline overview/>`

- [ ] **Step 1: Write the failing mute test**

```ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { isMuted, mute, readMutes } from "./mute.ts";

function memory(): Storage {
  const m = new Map<string, string>();
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => { m.set(k, v); } } as Storage;
}
const H = 3_600_000;

test("a mute lasts 24 hours", () => {
  const s = memory();
  mute("offline:lap", 0, () => s);
  const m = readMutes(() => s);
  assert.equal(isMuted("offline:lap", m, 23 * H), true);
  assert.equal(isMuted("offline:lap", m, 25 * H), false);
  assert.equal(isMuted("pins", m, 1), false);
});

test("blocked storage mutes nothing and never throws", () => {
  const broken = () => { throw new Error("blocked"); };
  assert.doesNotThrow(() => mute("pins", 0, broken));
  assert.deepEqual(readMutes(broken), {});
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `pnpm test`
Expected: FAIL — cannot find module `./mute.ts`.

- [ ] **Step 3: Implement `mute.ts`**

```ts
/** Needs-you items an operator muted, per browser, each for MUTE_MS. Storage
 *  that is blocked or throws mutes nothing. */

const KEY = "control.mutes";
export const MUTE_MS = 24 * 3_600_000;

export function readMutes(storage: () => Pick<Storage, "getItem"> = () => localStorage): Record<string, number> {
  try {
    const v = JSON.parse(storage().getItem(KEY) ?? "{}") as unknown;
    return v && typeof v === "object" ? (v as Record<string, number>) : {};
  } catch {
    return {};
  }
}

export function mute(key: string, now: number,
  storage: () => Pick<Storage, "getItem" | "setItem"> = () => localStorage): void {
  try {
    const live = Object.fromEntries(Object.entries(readMutes(storage)).filter(([, at]) => now - at < MUTE_MS));
    storage().setItem(KEY, JSON.stringify({ ...live, [key]: now }));
  } catch {
    // Unsaved: the item shows again.
  }
}

export function isMuted(key: string, mutes: Record<string, number>, now: number): boolean {
  const at = mutes[key];
  return at != null && now - at < MUTE_MS;
}
```

- [ ] **Step 4: Run the tests**

Run: `pnpm test`
Expected: all pass.

- [ ] **Step 5: Export the queue tables**

In `Queues.tsx`, change `function VerifyTable` to `export function VerifyTable` and `function FixTable` to `export function FixTable`. Delete `plural` and the whole `Queues` function, and the `useRef` import if nothing else uses it. Update the file's top doc comment so it no longer mentions the three cards.

- [ ] **Step 6: Write `StatusLine.tsx`**

```tsx
import { useState } from "react";
import { api, type ControlOverview } from "../../api";
import { statusLine } from "./overviewText";

/** One sentence for every machine and the AI hold, with Bring back on a
 *  machine an operator stood down. */
export function StatusLine({ overview, onChanged }: { overview: ControlOverview; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const off = overview.machines.filter((m) => m.stood_down);
  const up = async (host: string) => {
    setBusy(host);
    try { await api.standUp(host); onChanged(); } finally { setBusy(null); }
  };
  return (
    <div className="status-line">
      <span>{statusLine(overview)}</span>
      {off.map((m) => (
        <button key={m.host} className="linkish small" disabled={busy === m.host} onClick={() => up(m.host)}
          title={m.stood_down_note ? `Stood down: ${m.stood_down_note}` : `Clear the stand-down mark on ${m.host}`}>
          {busy === m.host ? "…" : `bring ${m.host} back`}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 7: Write `Attention.tsx`**

```tsx
import { useState } from "react";
import { Link } from "react-router";
import { api, type ControlOverview, type FilterSpec, type OverviewNeed } from "../../api";
import { ago } from "./format";
import { isMuted, mute, readMutes } from "./mute";

function NeedRow({ need, onChanged, onReviewParked, onMute }: {
  need: OverviewNeed; onChanged: () => void; onReviewParked: () => void; onMute: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setErr(null);
    try { await fn(); onChanged(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const action =
    need.kind === "trip" && need.host && need.lane
      ? <button className="btn-sm" disabled={busy} onClick={() => run(() => api.workerHealthResume(need.host!, need.lane!))}>{busy ? "Resuming…" : "Resume"}</button>
      : need.kind === "offline" && need.host
        ? <button className="btn-sm" disabled={busy} onClick={() => run(() => api.standDown(need.host!))}
            title="Mark this machine off on purpose. It reads as working again the next time one of its workers beats.">
            {busy ? "…" : "Stand down"}</button>
        : need.kind === "parked"
          ? <button className="btn-sm" onClick={onReviewParked}>Review</button>
          : null;
  const hasDetail = need.detail != null || need.groups.length > 0;
  return (
    <div className="attn-row">
      <div className="attn-head">
        <span>{need.title}</span>
        <span className="muted small">{need.at ? ago(need.at) : ""}</span>
        {hasDetail && <button className="linkish small" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{open ? "less" : "details"}</button>}
        <span style={{ flex: 1 }} />
        {err && <span className="activity-bad small">{err}</span>}
        {action}
        <button className="linkish small" onClick={onMute} title="Hide this for 24 hours in this browser">mute</button>
      </div>
      {open && (
        <div className="attn-detail muted small">
          {need.detail && <div style={{ whiteSpace: "pre-wrap" }}>{need.detail}</div>}
          {need.groups.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {need.groups.map((g) => {
                const spec: FilterSpec = { numbers: g.prs, state: "all" };
                return (
                  <Link key={`${g.lane}-${g.reason}`} className="chip chip-red sm"
                    to={`/explore?spec=${encodeURIComponent(JSON.stringify(spec))}`}>
                    {g.lane} · {g.reason.length > 60 ? `${g.reason.slice(0, 60)}…` : g.reason} ×{g.prs.length}
                  </Link>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** The problems a person must act on. Absent when there are none. */
export function Attention({ overview, onChanged, onReviewParked }: {
  overview: ControlOverview; onChanged: () => void; onReviewParked: () => void;
}) {
  const [mutes, setMutes] = useState<Record<string, number>>(() => readMutes());
  const now = Date.now();
  const shown = overview.needs.filter((n) => !isMuted(n.key, mutes, now));
  const muted = overview.needs.length - shown.length;
  if (shown.length === 0 && muted === 0) return null;
  return (
    <section className="cpanel tone-danger" aria-label="Needs you">
      <div className="cpanel-head">
        <h3 className="cpanel-title">Needs you · {shown.length}</h3>
        {muted > 0 && <span className="cpanel-meta">{muted} muted</span>}
      </div>
      <div className="cpanel-body">
        {shown.length === 0 ? <div className="muted small">Only muted items.</div> : shown.map((n) => (
          <NeedRow key={n.key} need={n} onChanged={onChanged} onReviewParked={onReviewParked}
            onMute={() => { mute(n.key, Date.now()); setMutes(readMutes()); }} />
        ))}
      </div>
    </section>
  );
}
```

`api.workerHealthResume` takes `(host, lane)`, where lane is the worker_health name. `need.lane` already carries that name for trips, as Task 4's `_need_rows` stores it.

- [ ] **Step 8: Write `Lanes.tsx`**

```tsx
import { Fragment, useEffect, useState } from "react";
import type { Autohunt, ControlOverview, FixQueue, OverviewLane, VerifyQueue } from "../../api";
import { FlyoutIssueLink } from "../../components/FlyoutIssueLink";
import { PRLink } from "../../components/PRLink";
import { ago, elapsed } from "./format";
import { cellLabel, nextText } from "./overviewText";
import { FixTable, VerifyTable } from "./Queues";

const TONE: Record<string, string> = {
  on: "chip-green", off: "chip-muted", tripped: "chip-red", "ai-paused": "chip-amber",
  "stood-down": "chip-muted", offline: "chip-red",
};

function NowCell({ lane, now }: { lane: OverviewLane; now: number }) {
  if (lane.now.length === 0) return <span className="muted">idle</span>;
  return (
    <>
      {lane.now.map((n, i) => (
        <div key={i}>
          {n.pr != null ? <PRLink n={n.pr} /> : n.issue != null ? <FlyoutIssueLink n={n.issue} /> : null}
          {n.step && <span className="muted"> · {n.step}</span>}
          {n.started_at && <span className="muted"> · {elapsed(n.started_at, now)}</span>}
          {n.host && <span className="muted small"> · {n.host}</span>}
        </div>
      ))}
    </>
  );
}

function Expanded({ lane, overview, verifyQueue, fixQueue, hunt, onFixChanged }: {
  lane: OverviewLane; overview: ControlOverview; verifyQueue: VerifyQueue | null; fixQueue: FixQueue | null;
  hunt: Autohunt | null; onFixChanged: () => Promise<unknown>;
}) {
  if (lane.lane === "verify" && verifyQueue) return <VerifyTable queue={verifyQueue} hunt={hunt} />;
  if (lane.lane === "autofix" && fixQueue) return <FixTable queue={fixQueue} onChanged={onFixChanged} />;
  const recent = overview.timeline.filter((t) => t.when === "past" && t.lane === lane.lane).slice(0, 10);
  return (
    <div className="small">
      {lane.queue.length > 0 && (
        <ol className="lane-queue">
          {lane.queue.map((q, i) => (
            <li key={i}>{q.pr != null ? <PRLink n={q.pr} /> : <FlyoutIssueLink n={q.issue!} />}
              {q.title && <span className="muted"> {q.title.slice(0, 70)}</span>}
              <span className="muted"> · {q.source ?? "auto"} · {ago(q.queued_at)}</span></li>
          ))}
        </ol>
      )}
      {recent.length === 0 ? <div className="muted">Nothing ended in the past day.</div>
        : recent.map((t, i) => (
          <div key={i} className={t.bad ? "activity-bad" : undefined}>
            <span className="muted">{new Date(t.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span> {t.text}
            {t.host && <span className="muted"> · {t.host}</span>}
          </div>
        ))}
    </div>
  );
}

/** One row per lane: its state on each machine, what it runs now and next,
 *  and its past day. A row opens its queue and recent endings. */
export function Lanes({ overview, open, onToggle, verifyQueue, fixQueue, hunt, onFixChanged }: {
  overview: ControlOverview; open: string | null; onToggle: (lane: string) => void;
  verifyQueue: VerifyQueue | null; fixQueue: FixQueue | null; hunt: Autohunt | null;
  onFixChanged: () => Promise<unknown>;
}) {
  const [now, setNow] = useState<number>(() => Date.now());
  const live = overview.lanes.some((l) => l.now.length > 0);
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(t);
  }, [live]);
  const many = overview.machines.length > 1;
  return (
    <section className="cpanel" aria-label="Lanes">
      <div className="cpanel-head"><h3 className="cpanel-title">Lanes</h3></div>
      <table className="grid compact lanes">
        <thead><tr><th>Lane</th><th>State</th><th>Now</th><th>Next</th><th title="Runs ended in the past 24 hours">24h</th></tr></thead>
        <tbody>
          {overview.lanes.map((l) => (
            <Fragment key={l.lane}>
              <tr className={`lane-row${open === l.lane ? " open" : ""}`} onClick={() => onToggle(l.lane)}
                aria-expanded={open === l.lane}>
                <td><span className={`caret ${open === l.lane ? "open" : ""}`}>▸</span> {l.label}</td>
                <td>{l.cells.length === 0 ? <span className="chip chip-muted sm">off</span> : l.cells.map((c) => (
                  <span key={c.host} className={`chip sm ${TONE[c.state]}`} title={c.host}>
                    {many ? `${c.host.split(/[.-]/)[0]}: ` : ""}{cellLabel(c.state)}
                  </span>
                ))}</td>
                <td><NowCell lane={l} now={now} /></td>
                <td className="muted">{nextText(l, overview.ai_paused?.retry_at ?? null)}
                  {l.parked > 0 && <b> · {l.parked} parked</b>}</td>
                <td>{l.ended_24h}{l.faults_24h > 0 && <span className="activity-bad"> ({l.faults_24h} failed)</span>}</td>
              </tr>
              {open === l.lane && (
                <tr><td colSpan={5}><Expanded lane={l} overview={overview} verifyQueue={verifyQueue}
                  fixQueue={fixQueue} hunt={hunt} onFixChanged={onFixChanged} /></td></tr>
              )}
            </Fragment>
          ))}
          <tr>
            <td className="muted">Background</td>
            <td colSpan={4} className="muted small">
              {overview.loops.length === 0 ? "no machine runs the periodic loops"
                : overview.loops.map((lp) => (
                  <span key={`${lp.host}-${lp.loop}`} className="loop-chip">
                    {lp.label}{many ? ` (${lp.host.split(/[.-]/)[0]})` : ""}: {lp.next_at
                      ? `next ${new Date(lp.next_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
                      : `last ${ago(lp.last_at)}`}
                  </span>
                ))}
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}
```

The Now cell shows the host after the elapsed time. `elapsed(startIso, nowMs)` already exists in `format.ts`; check its signature with `grep -n "export function elapsed" src/views/control/format.ts` and match it.

- [ ] **Step 9: Write `Timeline.tsx`**

```tsx
import type { ControlOverview, OverviewTimelineItem } from "../../api";
import { FlyoutIssueLink } from "../../components/FlyoutIssueLink";
import { PRLink } from "../../components/PRLink";

const LANE_LABEL: Record<string, string> = {
  security: "Security", verify: "Verify", design: "Design", autofix: "Autofix",
  "issue-fix": "Issue fix", cluster: "Clustering", background: "Background",
};

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function Line({ t }: { t: OverviewTimelineItem }) {
  const when = t.when === "now" ? "now" : t.when === "upcoming" && t.lane !== "background" ? "next" : clock(t.at);
  return (
    <div className={`tl-row tl-${t.when}${t.bad ? " activity-bad" : ""}`}>
      <span className="tl-when">{when}</span>
      <span className="tl-lane">{LANE_LABEL[t.lane] ?? t.lane}</span>
      <span className="tl-text">
        {t.pr != null ? <><PRLink n={t.pr} />{t.text.replace(/^#\d+/, "")}</>
          : t.issue != null ? <><FlyoutIssueLink n={t.issue} />{t.text.replace(/^#\d+/, "")}</>
            : t.text}
        {t.host && <span className="muted"> · {t.host}</span>}
      </span>
    </div>
  );
}

/** What runs next, what runs now, and what ran in the past day, in one list. */
export function Timeline({ overview }: { overview: ControlOverview }) {
  const upcoming = overview.timeline.filter((t) => t.when === "upcoming");
  const current = overview.timeline.filter((t) => t.when === "now");
  const past = overview.timeline.filter((t) => t.when === "past");
  return (
    <section className="cpanel" aria-label="Timeline">
      <div className="cpanel-head"><h3 className="cpanel-title">Timeline</h3>
        <span className="cpanel-meta">next hour · now · past 24 hours</span></div>
      <div className="cpanel-body timeline">
        {[...upcoming].reverse().map((t, i) => <Line key={`u${i}`} t={t} />)}
        <div className="tl-divider" role="separator">now</div>
        {current.map((t, i) => <Line key={`n${i}`} t={t} />)}
        {past.length === 0 ? <div className="muted small">Nothing ended in the past day.</div>
          : past.map((t, i) => <Line key={`p${i}`} t={t} />)}
      </div>
    </section>
  );
}
```

Upcoming items render soonest nearest the `now` divider: the list is reversed, so the latest is on top.

- [ ] **Step 10: Add the styles** — append to `styles.css` after the `.cpanel` block (around line 777)

```css
.status-line { display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline; color: var(--muted); font-size: 13px; margin: 0 0 14px; }
.attn-row { padding: 6px 0; border-top: 1px solid var(--border); }
.attn-row:first-child { border-top: none; }
.attn-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.attn-detail { margin: 4px 0 0; }
.lanes { width: 100%; }
.lanes .lane-row { cursor: pointer; }
.lanes .lane-row:hover { background: var(--panel); }
.lanes td .chip { margin-right: 4px; }
.lane-queue { margin: 0 0 8px 18px; padding: 0; }
.loop-chip { display: inline-block; margin-right: 14px; }
.timeline { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
.tl-row { display: grid; grid-template-columns: 56px 90px minmax(0, 1fr); gap: 8px; }
.tl-upcoming { color: var(--muted); }
.tl-when { color: var(--muted); font-variant-numeric: tabular-nums; }
.tl-divider { border-top: 1px dashed var(--border); color: var(--muted); font-size: 11px; text-align: center; margin: 4px 0; }
.ctabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin: 8px 0 12px; }
.ctabs button { all: unset; cursor: pointer; padding: 6px 12px; color: var(--muted); border-bottom: 2px solid transparent; }
.ctabs button[aria-selected="true"] { color: inherit; border-bottom-color: var(--accent); }
@media (max-width: 640px) { .lanes th:nth-child(4), .lanes td:nth-child(4) { display: none; } }
```

- [ ] **Step 11: Type-check and lint the new files**

Run (from `prospector_app/frontend`): `pnpm exec tsc -b && pnpm exec eslint src/views/control/{mute,overviewText,StatusLine,Attention,Lanes,Timeline,Queues}.ts*`
Expected: 0 tsc errors, and no lint errors in these files. `ControlPanel.tsx` still imports `Queues`, so tsc fails until Task 7. If it does, run Step 11 together with Task 7 Step 2 and commit the two tasks together.

- [ ] **Step 12: Commit**

```bash
git add prospector_app/frontend/src/views/control prospector_app/frontend/src/styles.css
git commit -m "Add the status line, Needs you, Lanes and Timeline sections"
```

---

### Task 7: Rewire the page

**Files:**
- Rewrite: `src/views/ControlPanel.tsx`
- Delete: `src/views/control/NeedsYou.tsx`, `src/views/control/RecentActivity.tsx`. Also delete `activitySentence.ts` and its test if nothing else imports them: `grep -rn "activitySentence" src | grep -v "control/activitySentence"`. `jobToOpen` lives there; keep the file if anything still uses it.

- [ ] **Step 1: Write the page**

```tsx
import { useCallback, useEffect, useState } from "react";
import { api, type Autohunt, type CapacityState, type ControlOverview, type FixQueue, type JobRuntime, type JobSpec, type PipelineStatus, type SuggestedAction, type VerifyQueue } from "../api";
import { usePoll } from "../poll";
import { Attention } from "./control/Attention";
import { Capacity } from "./control/Capacity";
import { Coverage } from "./control/Coverage";
import { JobLine } from "./control/JobLine";
import { Jobs } from "./control/Jobs";
import { Lanes } from "./control/Lanes";
import { RunHistory } from "./control/RunHistory";
import { StatusLine } from "./control/StatusLine";
import { Timeline } from "./control/Timeline";
import { RANGE_OPTIONS, type RangeOpt } from "./control/format";
import { useJobRunner } from "./control/useJobRunner";

type Tab = "jobs" | "capacity" | "history";
const TAB_KEY = "control.tab";

function readTab(): Tab {
  try {
    const v = localStorage.getItem(TAB_KEY);
    return v === "capacity" || v === "history" ? v : "jobs";
  } catch {
    return "jobs";
  }
}

/** Pipeline → Health & queues: every machine in a line, the problems that
 *  need a person, each lane's now and next, a timeline, and the secondary
 *  panels in tabs. */
export default function ControlPanel() {
  const [overview, setOverview] = useState<ControlOverview | null>(null);
  const [overviewErr, setOverviewErr] = useState<string>();
  const [open, setOpen] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>(readTab);
  const [specs, setSpecs] = useState<JobSpec[]>([]);
  const [runtimes, setRuntimes] = useState<Record<string, JobRuntime> | null>(null);
  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [suggestions, setSuggestions] = useState<SuggestedAction[]>([]);
  const [capacity, setCapacity] = useState<CapacityState | null>(null);
  const [hunt, setHunt] = useState<Autohunt | null>(null);
  const [verifyQueue, setVerifyQueue] = useState<VerifyQueue | null>(null);
  const [fixQueue, setFixQueue] = useState<FixQueue | null>(null);
  const [range, setRange] = useState<RangeOpt>(RANGE_OPTIONS[0]);

  const loadOverview = useCallback(() => api.controlOverview()
    .then((o) => { setOverview(o); setOverviewErr(undefined); })
    .catch((e) => setOverviewErr(String(e))), []);
  const loadPipeline = useCallback(() => {
    api.pipelineStatus().then(setPipeline).catch(() => {});
    api.suggestedActions("all").then((d) => setSuggestions(d.items)).catch(() => {});
    api.jobRuntimes().then((d) => setRuntimes(d.runtimes)).catch(() => {});
  }, []);
  const loadFixQueue = useCallback(
    () => api.fixQueue(range.days, range.allTime).then(setFixQueue).catch(() => {}), [range]);
  const loadVerifyQueue = useCallback(() => api.verifyQueue(7, false).then(setVerifyQueue).catch(() => {}), []);
  const loadHunt = useCallback(
    () => api.autohunt(range.days, range.allTime).then(setHunt).catch(() => {}), [range]);
  const loadCapacity = useCallback(() => api.capacity().then(setCapacity).catch(() => {}), []);
  const noop = useCallback(() => {}, []);

  const runner = useJobRunner(() => { loadPipeline(); loadOverview(); });
  useEffect(() => {
    api.jobSpecs().then((d) => setSpecs(d.specs)).catch(() => {});
    loadPipeline();
  }, [loadPipeline]);

  const busy = (overview?.lanes ?? []).some((l) => l.now.length > 0);
  usePoll(loadOverview, busy ? 5_000 : 15_000);
  usePoll(open === "autofix" || tab === "history" ? loadFixQueue : noop, 15_000);
  usePoll(open === "verify" || tab === "history" ? loadVerifyQueue : noop, 30_000);
  usePoll(open === "verify" || tab === "history" ? loadHunt : noop, 30_000);
  usePoll(tab === "capacity" ? loadCapacity : noop, 30_000);

  const pickTab = (t: Tab) => {
    setTab(t);
    try { localStorage.setItem(TAB_KEY, t); } catch { /* the tab resets next visit */ }
  };

  return (
    <div className="control">
      <div className="detail-head"><h1>🎛️ Health &amp; queues</h1></div>
      <JobLine runner={runner} />
      {overviewErr && !overview && <div className="error">Couldn't load the overview: {overviewErr}</div>}
      {overview == null ? <div className="muted small">Loading…</div> : (
        <>
          <StatusLine overview={overview} onChanged={loadOverview} />
          <Attention overview={overview} onChanged={loadOverview}
            onReviewParked={() => { setOpen("autofix"); loadFixQueue(); }} />
          <Lanes overview={overview} open={open}
            onToggle={(l) => setOpen((o) => (o === l ? null : l))}
            verifyQueue={verifyQueue} fixQueue={fixQueue} hunt={hunt} onFixChanged={loadFixQueue} />
          <Timeline overview={overview} />
        </>
      )}
      <div className="ctabs" role="tablist">
        {([["jobs", "Coverage and jobs"], ["capacity", "AI capacity"], ["history", "Run history"]] as const).map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => pickTab(k)}>{label}</button>
        ))}
      </div>
      {tab === "jobs" && (
        <>
          <Coverage pipeline={pipeline} />
          <Jobs specs={specs} runtimes={runtimes} suggestions={suggestions} pipeline={pipeline}
            running={runner.running}
            onStart={(url, spec) => {
              runner.attach(url, spec.kind, `▶ starting ${spec.label}…`);
              window.scrollTo({ top: 0, behavior: "smooth" });
            }} />
        </>
      )}
      {tab === "capacity" && <Capacity state={capacity} />}
      {tab === "history" && <RunHistory hunt={hunt} fixQueue={fixQueue} range={range} onRange={setRange} />}
    </div>
  );
}
```

Before relying on the `noop` swap, read `src/poll.ts`: `usePoll(fn, ms)` must re-subscribe when `fn` changes and run `fn` once on subscribe. If it does not run immediately, call the loader in the `onToggle` / `pickTab` handlers as well. `Capacity` and `RunHistory` were folded `Panel`s. Inside a tab they render their own `Panel` header, which is acceptable. If `Panel` folds them closed from a stored fold state, pass `foldable={false}` or set them to default open.

- [ ] **Step 2: Build, lint and test**

Run (from `prospector_app/frontend`): `pnpm run build && pnpm test && pnpm exec eslint src/views/ControlPanel.tsx src/views/control`
Expected: build passes with 0 tsc errors, tests pass, and no new lint errors in these files. Compare against `git stash`-free `git diff main --stat` to confirm only intended files changed.

- [ ] **Step 3: Commit**

```bash
git add -A prospector_app/frontend/src
git commit -m "Lay out Health & queues around lanes, now and next"
```

---

### Task 8: Verify in the preview and open the PR

- [ ] **Step 1: Run the full gates**

Run from the repo root: `uv run pytest && uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness && uv run ruff check .`
Expected: all green.

- [ ] **Step 2: Preview on a scratch store**

Start this worktree's backend and Vite configurations (`preview_start` by name from `.claude/launch.json`; run `./setup.sh` first if the file is missing). Make sure the backend uses a scratch store and no worker flags: override `TRIAGE_STORE_URL` to empty, so the SQLite default under `pipeline/store/` is used, and unset `TRIAGE_*_WORKER` (memory: a Studio worktree's `.env` starts live workers). Seed it with heartbeats and a trip so the page has content:

```bash
uv run python - <<'EOF'
from datetime import datetime, timedelta, timezone
from pipeline import worker_health
from prospector_app.backend import data
st = data.store()
now = datetime.now(timezone.utc)
st.save_verify_worker({"host": "studio", "last_beat": now.isoformat(), "pid": 1, "current_pr": None})
st.save_fix_worker({"host": "lap", "last_beat": (now - timedelta(hours=4)).isoformat(), "pid": 2})
st.save_worker_loops({"host": "studio", "at": now.isoformat(),
    "lanes": {"security": True, "verify": True, "design": True, "autofix": False, "issue-fix": True, "cluster": False},
    "loops": {"pr-watch": {"last_at": (now - timedelta(minutes=9)).isoformat(), "every_seconds": 900}}})
for _ in range(3):
    worker_health.update(st, "studio", lambda r: worker_health.record_failure(r, "design", kind="coverage", reason="sandbox"))
EOF
```

Open `/pipeline/control` and check:
- The status line reads `studio idle · lap offline`.
- Needs you shows the design trip and `lap worker offline`.
- Clicking **Stand down** on lap makes the status line read `lap off`, removes the offline row, and clears lap from the red strip at the top.
- The Lanes cells read Autofix and Clustering `off`, and Design `tripped`.
- Background shows `PR watch: next HH:MM`.
- Mute hides a row and shows `1 muted`.
- The tabs switch, and the choice survives a reload.

Use `read_console_messages` to confirm there are no errors, and `resize_window` mobile to confirm no horizontal scroll. Take a screenshot as proof.

- [ ] **Step 3: Push and open the PR**

Commit and push as separate Bash calls with no `cd` (memory: the push guard). Open the PR against `main` with `gh pr create`. In the body, cover:
- the summary;
- the four spec refinements;
- the test plan;
- that PR 2 (pause and reorder) and PR 3 (log tail) follow;
- the attribution line.

Then call `get_status` / `bind_pr` per the session rules.
