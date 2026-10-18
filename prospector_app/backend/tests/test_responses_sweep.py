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
def scans(monkeypatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(responses, "scan", lambda: calls.append(True) or RESULT)
    return calls


def test_a_pass_scans_and_books_the_ledger(store, scans):
    assert responses_sweep.sweep_once(NOW) == RESULT
    assert scans == [True]
    rec = data.latest_run(responses_sweep.PHASE)
    assert rec is not None and rec.raw["stats"] == {"checked": 3, "with_response": 1, "failed": 1}
    assert rec.raw["trigger"] == "worker"


def test_a_recent_pass_is_not_repeated(store, scans):
    at = (NOW - timedelta(minutes=10)).isoformat()
    store.append_run({"phase": responses_sweep.PHASE, "started": at, "finished": at,
                      "trigger": "worker", "stats": {}})
    data.refresh()
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
