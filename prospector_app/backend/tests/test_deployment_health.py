"""deployment health: tripped lanes and dark workers are named per machine,
stale ingest crosses amber then red, and the auto column reads stalled only
when no live, untripped worker is left on any known lane."""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import worker_health
from pipeline import store as S
from prospector_app.backend import data, deployment_health


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    monkeypatch.setattr(deployment_health, "_issue_ingest_at", lambda: None)
    return st


def _iso(ago_seconds: float = 0.0) -> str:
    at = datetime.now(timezone.utc) - timedelta(seconds=ago_seconds)
    return at.isoformat(timespec="seconds")


def test_empty_deployment_is_healthy(store):
    got = deployment_health.compute()
    assert got == {"level": "ok", "items": [], "auto_stalled": False,
                   "stalled_reason": None, "lanes_down": []}


def test_tripped_lanes_name_the_lanes_machine_and_kind(store):
    store.save_verify_worker({"host": "studio", "last_beat": _iso()})
    store.save_fix_worker({"host": "laptop", "last_beat": _iso()})
    def _trip(r: dict) -> None:
        worker_health.trip(r, "security", kind="agent-unavailable", reason="CLI missing")
        worker_health.trip(r, "verify", kind="agent-unavailable", reason="CLI missing")
    worker_health.update(store, "studio", _trip)

    got = deployment_health.compute()
    assert got["level"] == "red"
    (item,) = got["items"]
    assert item["text"] == "security, verify lanes paused on studio (agent-unavailable)"
    assert item["detail"] == "CLI missing"
    assert item["to"] == "/control"
    # The fix worker on laptop is live and untripped, so the deployment still
    # drains work: down lanes are listed but the auto column is not stalled.
    assert got["lanes_down"] == ["security", "verify"]
    assert got["auto_stalled"] is False


def test_every_known_lane_down_reads_stalled(store):
    store.save_verify_worker({"host": "studio", "last_beat": _iso()})
    store.save_fix_worker({"host": "studio", "last_beat": _iso()})
    def _trip(r: dict) -> None:
        for lane in worker_health.LANES:
            worker_health.trip(r, lane, kind="agent-unavailable", reason="auth expired")
    worker_health.update(store, "studio", _trip)

    got = deployment_health.compute()
    assert got["lanes_down"] == ["security", "verify", "fix"]
    assert got["auto_stalled"] is True
    assert got["stalled_reason"] is not None and "no live" in got["stalled_reason"]


def test_dark_worker_is_an_offline_item_and_downs_its_lanes(store):
    two_hours = 2 * 3600
    store.save_verify_worker({"host": "studio", "last_beat": _iso(two_hours)})
    got = deployment_health.compute()
    (item,) = got["items"]
    assert item["level"] == "red"
    assert item["text"] == "worker studio offline · last beat 2h ago"
    # The only known worker is dark, so every lane it ran is down and nothing
    # else is left to drain the queues.
    assert got["lanes_down"] == ["security", "verify"]
    assert got["auto_stalled"] is True


def test_recently_quiet_worker_downs_lanes_without_an_item(store):
    # Quiet past the heartbeat staleness bar but short of the offline
    # escalation threshold: the lane is down, the strip has nothing yet.
    store.save_fix_worker({"host": "studio", "last_beat": _iso(600)})
    got = deployment_health.compute()
    assert got["items"] == []
    assert got["lanes_down"] == ["fix"]
    assert got["auto_stalled"] is True


def test_stale_ingest_crosses_amber_then_red(store, monkeypatch):
    monkeypatch.setattr(deployment_health, "_pr_ingest_at", lambda: _iso(2 * 86400))
    got = deployment_health.compute()
    (item,) = got["items"]
    assert got["level"] == "amber"
    assert item["level"] == "amber"
    assert item["text"] == "PR ingest 2d old"

    monkeypatch.setattr(deployment_health, "_pr_ingest_at", lambda: _iso(19 * 86400))
    got = deployment_health.compute()
    (item,) = got["items"]
    assert got["level"] == "red"
    assert item["text"] == "PR ingest 19d old"


def test_fresh_ingest_from_the_runs_ledger_says_nothing(store):
    store.append_run({"phase": "ingest", "started": _iso(120), "finished": _iso(60)})
    got = deployment_health.compute()
    assert got["items"] == [] and got["level"] == "ok"


def test_old_ingest_run_in_the_ledger_is_read(store):
    old = _iso(5 * 86400)
    store.append_run({"phase": "ingest", "started": old, "finished": old})
    got = deployment_health.compute()
    (item,) = got["items"]
    assert item["key"] == "ingest:pr" and item["level"] == "red"
