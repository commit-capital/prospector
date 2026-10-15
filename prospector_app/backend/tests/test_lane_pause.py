"""An operator's pause on one lane of one machine."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline import store as S
from pipeline import worker_health
from prospector_app.backend import data, lane_health, lane_pause


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_WORKER_ID", "mac-1")
    return st


def test_pause_is_per_host(store):
    lane_pause.pause("mac-1", "verify", by="ops")
    assert lane_pause.paused("mac-1", "verify") is True
    assert lane_pause.paused("mac-2", "verify") is False
    assert lane_pause.paused("mac-1", "fix") is False
    assert lane_pause.paused_lanes(store.load_lane_pauses()) == {"mac-1": {"verify"}}


def test_paused_lane_is_closed_and_books_nothing(store):
    lane_pause.pause("mac-1", "fix", by="ops")
    assert lane_health.open_or_retest("fix") is False
    assert lane_health.open_or_retest("verify") is True
    rec = worker_health.load(store, "mac-1")
    assert not worker_health.is_tripped(rec, "fix")
    assert (worker_health.lane(rec, "fix").get("consecutive_failures") or 0) == 0


def test_resume_reopens(store):
    lane_pause.pause("mac-1", "fix", by="ops")
    lane_pause.resume("mac-1", "fix")
    assert lane_health.open_or_retest("fix") is True
    assert store.load_lane_pauses()["mac-1"]["lanes"] == {}


def test_unknown_lane_is_refused(store):
    with pytest.raises(ValueError):
        lane_pause.pause("mac-1", "nope", by="ops")


def test_endpoints(store, monkeypatch):
    from prospector_app.backend import app as app_mod
    monkeypatch.setenv("TRIAGE_REPO", "owner/name")
    client = TestClient(app_mod.app)
    assert client.post("/api/worker/lanes/verify/pause", json={"host": "mac-1"}).status_code == 200
    assert lane_pause.paused("mac-1", "verify")
    assert client.post("/api/worker/lanes/verify/resume", json={"host": "mac-1"}).status_code == 200
    assert not lane_pause.paused("mac-1", "verify")
    assert client.post("/api/worker/lanes/nope/pause", json={"host": "mac-1"}).status_code == 400
    assert client.post("/api/worker/lanes/verify/pause", json={}).status_code == 400


def test_a_paused_lane_tripped_on_the_agent_pushes_nothing(store, monkeypatch):
    from prospector_app.backend import fix_worker, worker_selftest
    agent_kind = sorted(worker_selftest.AGENT_KINDS)[0]
    worker_health.update(store, "mac-1", lambda r: worker_health.trip(
        r, "fix", kind=agent_kind, reason="claude CLI is not authenticated"))
    assert fix_worker.pushes_open(False) is True
    lane_pause.pause("mac-1", "fix", by="ops")
    assert fix_worker.pushes_open(False) is False
