"""responses_sweep: a worker keeps the stored author responses current, one
machine at a time, and books each pass in the runs ledger."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import store as S
from prospector_app.backend import data, responses, responses_sweep

NOW = datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)
RESULT = {"checked": 3, "with_response": 1, "prs": [1], "issues": [],
          "failed": [2], "failed_issues": []}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    return st


@pytest.fixture
def scans(monkeypatch) -> list[str | None]:
    """The `since` each scan was asked for."""
    calls: list[str | None] = []
    monkeypatch.setattr(responses, "scan", lambda *, since: calls.append(since) or RESULT)
    return calls


def _book(store: S.Store, started: datetime, **stats) -> None:
    store.append_run({"phase": responses_sweep.PHASE, "started": started.isoformat(),
                      "finished": started.isoformat(), "trigger": "worker", "stats": stats})
    data.refresh()


def test_a_pass_scans_and_books_the_ledger(store, scans):
    assert responses_sweep.sweep_once(NOW) == RESULT
    rec = data.latest_run(responses_sweep.PHASE)
    assert rec is not None and rec.raw["stats"] == {"checked": 3, "with_response": 1, "failed": 1,
                                                    "listed_from": None}
    assert rec.raw["trigger"] == "worker"


def test_the_first_pass_reads_everything(store, scans):
    responses_sweep.sweep_once(NOW)
    assert scans == [None]


def test_a_clean_pass_lists_from_its_start(store, scans):
    _book(store, NOW - timedelta(hours=1), failed=0, listed_from=None)
    responses_sweep.sweep_once(NOW)
    assert scans == ["2026-10-02T16:55:00Z"]


def test_after_a_failed_read_the_next_pass_lists_from_where_that_one_did(store, scans):
    _book(store, NOW - timedelta(hours=1), failed=2, listed_from="2026-10-02T12:00:00Z")
    responses_sweep.sweep_once(NOW)
    assert scans == ["2026-10-02T12:00:00Z"]


def test_a_recent_pass_is_not_repeated(store, scans):
    _book(store, NOW - timedelta(minutes=10))
    assert responses_sweep.sweep_once(NOW) is None
    assert scans == []


def test_another_machines_lease_holds_the_pass(store, scans, monkeypatch):
    assert store.claim_lease(responses_sweep.LEASE, host="other", seconds=3600)
    assert responses_sweep.sweep_once(NOW) is None
    assert scans == []


def test_the_lease_is_released_after_a_pass(store, scans):
    responses_sweep.sweep_once(NOW)
    assert store.claim_lease(responses_sweep.LEASE, host="other", seconds=60)


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_WORKER", "1")
    monkeypatch.setenv("TRIAGE_RESPONSES_SWEEP", "0")
    assert responses_sweep.enabled() is False
