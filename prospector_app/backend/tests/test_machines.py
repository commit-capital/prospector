"""The machine roster (#323): heartbeats, lane health, and base pins from the
shared store composed into one per-machine list."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import store as S
from pipeline import worker_health
from prospector_app.backend import data
from prospector_app.backend import machines


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_WORKER_ID", "local-1")
    return st


def test_roster_composes_beats_health_and_pins(store):
    store.save_verify_worker({"host": "mac-1", "last_beat": _iso(0.1), "pid": 1})
    store.save_fix_worker({"host": "mac-1", "last_beat": _iso(0.1), "pid": 1,
                           "current_pr": 42, "autohunt": True})
    store.save_verify_worker({"host": "mac-2", "last_beat": _iso(600), "pid": 2})
    worker_health.update(store, "mac-2", lambda r: worker_health.record_failure(
        r, "verify", kind="crash", reason="crashed"))

    out = machines.roster()
    assert out["local"] == "local-1"
    by_host = {m["host"]: m for m in out["machines"]}
    assert set(by_host) == {"mac-1", "mac-2"}

    m1 = by_host["mac-1"]
    assert m1["online"] is True
    assert m1["beats"]["verify"]["online"] is True
    assert m1["beats"]["fix"]["current_pr"] == 42
    assert m1["beats"]["fix"]["autohunt"] is True

    m2 = by_host["mac-2"]
    assert m2["online"] is False
    assert m2["beats"]["verify"]["online"] is False
    assert m2["lanes"]["verify"]["consecutive_failures"] == 1
    assert m2["lanes"]["verify"]["tripped"] is False

    # Online machines sort first.
    assert [m["host"] for m in out["machines"]] == ["mac-1", "mac-2"]


def test_roster_marks_base_pinned_hosts(store):
    store.save_verify_base({"host": "mac-3", "base_sha": "a" * 40, "tier": 1,
                            "baseline_failing": [], "baseline_captured_at": _iso(60)})
    out = machines.roster()
    assert [m["host"] for m in out["machines"]] == ["mac-3"]
    assert out["machines"][0]["base_pinned"] is True
    assert out["machines"][0]["beats"] == {}


def test_roster_empty_store(store):
    assert machines.roster() == {"machines": [], "local": "local-1"}
