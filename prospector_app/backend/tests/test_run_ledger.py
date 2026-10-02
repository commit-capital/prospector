"""The in-memory runs ledger: whole on its first read, then only the rows it
does not hold; concurrent callers share a read; a disk copy seeds the first
read when the store still has its newest row; `data.runs` answers windowed
reads from it once it is held."""
from __future__ import annotations

import threading
import time
from collections.abc import Iterable

import pytest

from pipeline import storekit
from pipeline.store import Store
from prospector_app.backend import data, run_ledger

OLD = "2020-01-01T00:00:00+00:00"


def _row(rowid: int, phase: str, ts: str = "2026-06-01T00:00:00+00:00") -> storekit.LedgerRow:
    return storekit.LedgerRow(rowid, ts, storekit.parse_run({"phase": phase, "stats": {}}))


class FakeSource:
    def __init__(self, *rows: storekit.LedgerRow) -> None:
        self.rows = list(rows)
        self.asked: list[tuple[int | None, list[int]]] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.fail = False

    def runs_after(self, rowid: int | None, held: Iterable[int] = ()) -> list[storekit.LedgerRow]:
        have = sorted(held)
        self.asked.append((rowid, have))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise ConnectionError("pooler went away")
        return sorted((r for r in self.rows
                       if (rowid is None or r.rowid > rowid) and r.rowid not in have),
                      key=lambda r: r.rowid)


def _phases(rows: list[storekit.LedgerRow]) -> list[str]:
    return [r.record.phase for r in rows if isinstance(r.record, storekit.PhaseRun)]


def test_the_first_read_takes_the_whole_ledger():
    src = FakeSource(_row(1, "ingest"), _row(2, "cluster"))
    ledger = run_ledger.RunLedger(src)
    assert not ledger.loaded
    assert _phases(ledger.rows()) == ["ingest", "cluster"]
    assert ledger.loaded
    assert src.asked == [(None, [])]


def test_an_empty_ledger_reads_as_held():
    ledger = run_ledger.RunLedger(FakeSource())
    assert ledger.rows() == []
    assert ledger.loaded


def test_a_later_read_names_the_overlap_rows_it_holds(monkeypatch):
    monkeypatch.setattr(run_ledger, "OVERLAP", 2)
    src = FakeSource(*(_row(n, f"p{n}") for n in range(1, 5)))
    ledger = run_ledger.RunLedger(src)
    ledger.rows()
    src.rows.append(_row(5, "p5"))
    assert _phases(ledger.rows()) == ["p1", "p2", "p3", "p4", "p5"]
    assert src.asked[1] == (2, [3, 4])
    ledger.rows()
    assert src.asked[2] == (3, [4, 5])


def test_a_row_committed_after_a_higher_one_is_picked_up_in_order():
    src = FakeSource(_row(1, "a"), _row(2, "b"), _row(4, "d"))
    ledger = run_ledger.RunLedger(src)
    ledger.rows()
    src.rows.append(_row(3, "c"))
    assert _phases(ledger.rows()) == ["a", "b", "c", "d"]


def test_callers_arriving_during_a_read_share_the_next_one():
    src = FakeSource(_row(1, "ingest"))
    ledger = run_ledger.RunLedger(src)
    src.gate = threading.Event()
    first = threading.Thread(target=ledger.rows)
    first.start()
    assert src.entered.wait(5)
    src.rows.append(_row(2, "cluster"))
    got: list[list[str]] = []
    waiters = [threading.Thread(target=lambda: got.append(_phases(ledger.rows())))
               for _ in range(3)]
    for t in waiters:
        t.start()
    time.sleep(0.2)
    src.gate.set()
    for t in [first, *waiters]:
        t.join(5)
    assert len(src.asked) == 2
    assert got == [["ingest", "cluster"]] * 3


def test_a_failed_read_raises_and_the_next_caller_reads_again():
    src = FakeSource(_row(1, "ingest"))
    ledger = run_ledger.RunLedger(src)
    src.fail = True
    with pytest.raises(ConnectionError):
        ledger.rows()
    assert not ledger.loaded
    src.fail = False
    assert _phases(ledger.rows()) == ["ingest"]
    assert src.asked == [(None, []), (None, [])]


class FakeCopy:
    def __init__(self, *rows: storekit.LedgerRow) -> None:
        self.rows: list[storekit.LedgerRow] | None = list(rows) or None
        self.saved: list[list[storekit.LedgerRow]] = []

    def load(self) -> list[storekit.LedgerRow] | None:
        return self.rows

    def save(self, rows: list[storekit.LedgerRow]) -> bool:
        self.saved.append(rows)
        return True


def _settle(ledger: run_ledger.RunLedger) -> None:
    writer = ledger._copier
    if writer is not None:
        writer.join(5)


def test_a_copy_seeds_the_first_read_which_reads_only_past_it(monkeypatch):
    monkeypatch.setattr(run_ledger, "OVERLAP", 2)
    src = FakeSource(*(_row(n, f"p{n}") for n in range(1, 7)))
    copy = FakeCopy(*(_row(n, f"p{n}") for n in range(1, 6)))
    ledger = run_ledger.RunLedger(src, copy)
    assert _phases(ledger.rows()) == ["p1", "p2", "p3", "p4", "p5", "p6"]
    assert ledger.loaded
    assert src.asked == [(3, [4])]
    ledger.rows()
    assert src.asked[1] == (4, [5, 6])


def test_the_copy_is_taken_only_after_the_store_answers(monkeypatch):
    src = FakeSource(_row(1, "ingest"))
    ledger = run_ledger.RunLedger(src, FakeCopy(_row(1, "ingest")))
    src.fail = True
    with pytest.raises(ConnectionError):
        ledger.rows()
    assert not ledger.loaded
    src.fail = False
    assert _phases(ledger.rows()) == ["ingest"]


@pytest.mark.parametrize("store_rows", [
    [_row(1, "a"), _row(2, "b")],
    [_row(1, "a"), _row(2, "b"), _row(3, "c", ts="2026-09-01T00:00:00+00:00")],
], ids=["newest-row-gone", "newest-row-other-ts"])
def test_a_copy_the_store_does_not_vouch_for_is_discarded(store_rows):
    src = FakeSource(*store_rows)
    copy = FakeCopy(_row(1, "x"), _row(2, "y"), _row(3, "z"))
    ledger = run_ledger.RunLedger(src, copy)
    assert _phases(ledger.rows()) == [r.record.phase for r in store_rows]
    assert src.asked[-1] == (None, [])
    _settle(ledger)
    assert [_phases(rows) for rows in copy.saved] == [[r.record.phase for r in store_rows]]


def test_no_usable_copy_reads_the_whole_ledger_and_writes_one():
    src = FakeSource(_row(1, "ingest"), _row(2, "cluster"))
    copy = FakeCopy()
    ledger = run_ledger.RunLedger(src, copy)
    assert _phases(ledger.rows()) == ["ingest", "cluster"]
    assert src.asked == [(None, [])]
    _settle(ledger)
    assert [_phases(rows) for rows in copy.saved] == [["ingest", "cluster"]]
    assert [r.ts for r in copy.saved[0]] == [r.ts for r in src.rows]


def test_the_copy_is_rewritten_only_when_the_ledger_grew_and_not_too_often(monkeypatch):
    src = FakeSource(_row(1, "a"))
    copy = FakeCopy(_row(1, "a"))
    ledger = run_ledger.RunLedger(src, copy)
    monkeypatch.setattr(run_ledger, "COPY_EVERY", 0.0)
    ledger.rows()
    _settle(ledger)
    assert copy.saved == []
    src.rows.append(_row(2, "b"))
    monkeypatch.setattr(run_ledger, "COPY_EVERY", 3600.0)
    ledger.rows()
    _settle(ledger)
    assert copy.saved == []
    monkeypatch.setattr(run_ledger, "COPY_EVERY", 0.0)
    ledger.rows()
    _settle(ledger)
    assert [_phases(rows) for rows in copy.saved] == [["a", "b"]]


@pytest.fixture
def store(tmp_path, monkeypatch) -> Store:
    st = Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    return st


def _append(st: Store, phase: str, ts: str | None = None) -> None:
    rec: dict = {"phase": phase, "stats": {}}
    if ts is not None:
        rec["ts"] = ts
    st.append_run(rec)


def test_the_ledger_follows_the_store(store, tmp_path, monkeypatch):
    _append(store, "ingest")
    assert _phases_of(data.runs()) == ["ingest"]
    other = Store(tmp_path / "other")
    monkeypatch.setattr(data, "_store", other)
    assert data.runs() == []


def _phases_of(records: list[storekit.RunRecord]) -> list[str]:
    return [r.phase for r in records if isinstance(r, storekit.PhaseRun)]


def test_a_windowed_read_asks_the_store_until_the_ledger_is_held(store, monkeypatch):
    _append(store, "ingest")
    asked: list[tuple[int | None, str | None]] = []
    real = store.runs
    monkeypatch.setattr(store, "runs", lambda limit=None, since=None: asked.append(
        (limit, since)) or real(limit=limit, since=since))
    assert _phases_of(data.runs(since=OLD)) == ["ingest"]
    assert asked == [(None, OLD)]


def test_a_windowed_read_is_served_from_the_held_ledger(store, monkeypatch):
    _append(store, "ingest", OLD)
    data.runs()
    monkeypatch.setattr(store, "runs", lambda **_: pytest.fail("read the window from the store"))
    _append(store, "cluster")
    assert _phases_of(data.runs(since="2026-01-01T00:00:00+00:00")) == ["cluster"]
    assert _phases_of(data.runs(limit=1)) == ["cluster"]


@pytest.mark.parametrize("since", [None, OLD, "2025-06-01T00:00:00+00:00",
                                   "2026-06-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"])
@pytest.mark.parametrize("limit", [None, 0, 1, 2, 10])
def test_the_held_ledger_answers_as_the_store_does(store, since, limit):
    """A row appended late with an old `ts` (a backfill) sits past newer ones:
    the window is the `ts` column, not the rowid."""
    for phase, ts in (("a", OLD), ("b", "2026-06-01T00:00:00+00:00"),
                      ("c", "2026-06-01T00:00:00.5+00:00"), ("d", "2025-01-01T00:00:00+00:00"),
                      ("e", "2026-07-01T00:00:00+00:00")):
        _append(store, phase, ts)
    data.runs()
    assert (_phases_of(data.runs(limit=limit, since=since))
            == _phases_of(store.runs(limit=limit, since=since)))


@pytest.mark.parametrize("family", ["issue", "alert"])
def test_the_issue_and_alert_ledgers_read_only_the_rows_they_lack(family, tmp_path, monkeypatch):
    from alert_triage.alert_store import AlertStore
    from issue_triage.issue_store import IssueStore
    from prospector_app.backend import alert_data, issue_data
    module, cls = (issue_data, IssueStore) if family == "issue" else (alert_data, AlertStore)
    module.set_store_root(tmp_path / family)
    try:
        st = module.store()
        st.append_run({"phase": "ingest", "stats": {}})
        assert _phases_of(module.runs()) == ["ingest"]
        asked: list[tuple[int | None, list[int]]] = []
        real = cls.runs_after
        monkeypatch.setattr(cls, "runs_after", lambda self, rowid, held=(): asked.append(
            (rowid, sorted(held))) or real(self, rowid, held))
        monkeypatch.setattr(cls, "runs", lambda self: pytest.fail("re-read the whole ledger"))
        st.append_run({"phase": "analyze", "stats": {}})
        module._runs_snapshot.refresh()
        assert _phases_of(module.runs()) == ["ingest", "analyze"]
        assert asked == [(1 - run_ledger.OVERLAP, [1])]
    finally:
        module.set_store_root(None)
