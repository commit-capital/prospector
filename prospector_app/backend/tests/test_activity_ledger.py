"""The activity log held in memory: the first read takes the whole table, a
read within the debounce is served from memory, a refresh reads only the events
the copy lacks, an event this process records is in its next read, and a
restarted process starts from its disk copy."""
from __future__ import annotations

from collections.abc import Iterable

import pytest

from pipeline import schema
from pipeline import storekit
from prospector_app.backend import activity, run_ledger


def _insert(url: str, *events: dict) -> None:
    eng = storekit.get_engine(url)
    with eng.begin() as conn:
        for ev in events:
            conn.execute(schema.activity.insert().values(**schema.activity_row(ev)))


def _ev(n: int) -> dict:
    return {"at": f"2026-07-17T10:{n:02d}:00+00:00", "kind": "comment", "pr": n,
            "status": "executed", "dry_run": False}


def _prs(events: list[dict]) -> list[int]:
    return [ev["pr"] for ev in events]


@pytest.fixture
def asked(monkeypatch) -> list[tuple[int | None, list[int], int]]:
    """Every read the log makes of its table: the rowid it reads past, the
    rowids it names as held, and how many events came back."""
    calls: list[tuple[int | None, list[int], int]] = []
    real = activity.ActivityTable.runs_after

    def spy(self: activity.ActivityTable, rowid: int | None,
            held: Iterable[int] = ()) -> list[storekit.LedgerRow[dict]]:
        have = sorted(held)
        rows = real(self, rowid, have)
        calls.append((rowid, have, len(rows)))
        return rows

    monkeypatch.setattr(activity.ActivityTable, "runs_after", spy)
    return calls


def test_a_read_within_the_debounce_is_served_from_memory(temp_store, asked):
    _insert(temp_store, _ev(1))
    assert _prs(activity.all_events()) == [1]
    _insert(temp_store, _ev(2))
    assert _prs(activity.all_events()) == [1]
    assert asked == [(None, [], 1)]


def test_a_refresh_takes_only_the_events_it_lacks(temp_store, asked):
    _insert(temp_store, _ev(1), _ev(2))
    assert _prs(activity.all_events()) == [2, 1]
    _insert(temp_store, _ev(3))
    activity.refresh()
    assert _prs(activity.all_events()) == [3, 2, 1]
    assert asked == [(None, [], 2), (2 - run_ledger.OVERLAP, [1, 2], 1)]


def test_an_event_recorded_after_a_read_is_in_the_next_one(temp_store):
    assert activity.all_events() == []
    activity.record("comment", pr=7, status="executed", dry_run=False)
    activity.record("comment", pr=8, status="executed", dry_run=False)
    assert _prs(activity.all_events()) == [8, 7]


def test_legacy_rows_are_held_normalized(temp_store):
    _insert(temp_store, {"at": "2026-07-17T10:00:00+00:00", "kind": "execute",
                         "action": "CLOSE_DUP", "pr": 4})
    [ev] = activity.all_events()
    assert (ev["kind"], ev["reason"]) == ("close", "duplicate")


def test_a_restarted_process_reads_only_past_its_disk_copy(temp_store, asked, tmp_path, monkeypatch):
    monkeypatch.setenv("PROSPECTOR_CACHE_DIR", str(tmp_path / "cache"))
    _insert(temp_store, _ev(1), _ev(2))
    activity.all_events()
    writer = activity._held_log().ledger._copier
    if writer is not None:
        writer.join(5)
    monkeypatch.setattr(activity, "_log", None)
    _insert(temp_store, _ev(3))
    assert _prs(activity.all_events()) == [3, 2, 1]
    assert asked[-1] == (2 - run_ledger.OVERLAP, [1], 2)


def test_the_log_follows_the_store(temp_store, tmp_path, monkeypatch):
    _insert(temp_store, _ev(1))
    assert _prs(activity.all_events()) == [1]
    other = f"sqlite:///{tmp_path}/other.db"
    schema.METADATA.create_all(storekit.get_engine(other))
    monkeypatch.setenv("TRIAGE_STORE_URL", other)
    assert activity.all_events() == []
