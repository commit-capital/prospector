"""health_strip: tripped lanes and silent workers read red, ingest staleness
grades amber then red, a quiet store shows nothing, and `stalled` answers
whether any lane anywhere can still pick work."""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import worker_health
from pipeline import store as S
from prospector_app.backend import data, health_strip


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(health_strip, "_issue_ingest_at", lambda: None)
    monkeypatch.setattr(health_strip, "_alert_ingest_at", lambda: None)
    data.refresh()
    return st


def _iso(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def test_quiet_store_shows_nothing(store):
    st = health_strip.status()
    assert st == {"items": [], "stalled": False}


def test_tripped_lanes_read_red_grouped_per_host(store):
    worker_health.update(store, "studio", lambda r: worker_health.record_success(r, "security"))
    worker_health.update(store, "studio", lambda r: worker_health.trip(
        r, "verify", kind="pin-refresh", reason="base image will not build"))
    worker_health.update(store, "studio", lambda r: worker_health.trip(
        r, "fix", kind="agent-unavailable", reason="claude CLI not authenticated"))
    items = health_strip.status()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "lane-tripped" and it["severity"] == "red"
    assert it["host"] == "studio" and it["link"] == "/control"
    assert it["text"].startswith("2/3 lanes paused on studio")
    assert "agent-unavailable" in it["text"] and "pin-refresh" in it["text"]
    assert "claude CLI not authenticated" in (it["detail"] or "")


def test_offline_worker_reads_red_and_a_beating_one_does_not(store):
    store.save_verify_worker({"host": "studio", "last_beat": _iso(hours=5)})
    store.save_fix_worker({"host": "laptop", "last_beat": _iso(seconds=10)})
    items = health_strip.status()["items"]
    assert [i["kind"] for i in items] == ["worker-offline"]
    assert items[0]["host"] == "studio" and items[0]["severity"] == "red"
    assert "last beat 5h ago" in items[0]["text"]


def test_pr_ingest_staleness_grades_amber_then_red(store):
    store.append_run({"phase": "ingest", "started": _iso(days=5, minutes=10),
                      "finished": _iso(days=5)})
    items = health_strip.status()["items"]
    assert [(i["kind"], i["severity"]) for i in items] == [("ingest-stale", "red")]
    assert items[0]["text"] == "PR ingest 5d old"

    store.append_run({"phase": "ingest", "started": _iso(days=2, minutes=10),
                      "finished": _iso(days=2)})
    items = health_strip.status()["items"]
    assert [(i["kind"], i["severity"]) for i in items] == [("ingest-stale", "amber")]
    assert items[0]["text"] == "PR ingest 2d old"

    store.append_run({"phase": "ingest", "started": _iso(hours=2), "finished": _iso(hours=1)})
    assert health_strip.status()["items"] == []


def test_pr_ingest_never_run_is_red_only_once_there_are_prs(store, monkeypatch):
    assert health_strip.status()["items"] == []
    monkeypatch.setattr(data, "prs", lambda: {1: object()})
    items = health_strip.status()["items"]
    assert len(items) == 1 and items[0]["severity"] == "red"
    assert "PR ingest not run" in items[0]["text"]


def test_issue_and_alert_ingest_use_their_own_thresholds(store, monkeypatch):
    issue_at, alert_at = _iso(days=4, minutes=5), _iso(days=32, minutes=5)
    monkeypatch.setattr(health_strip, "_issue_ingest_at", lambda: issue_at)
    monkeypatch.setattr(health_strip, "_alert_ingest_at", lambda: alert_at)
    labels = {i["text"]: i["severity"] for i in health_strip.status()["items"]}
    assert labels == {"issue ingest 4d old": "amber", "alert ingest 32d old": "red"}


def test_red_items_sort_before_amber(store, monkeypatch):
    monkeypatch.setattr(health_strip, "_issue_ingest_at", lambda: _iso(days=4))
    worker_health.update(store, "studio", lambda r: worker_health.trip(
        r, "fix", kind="sandbox", reason="docker down"))
    severities = [i["severity"] for i in health_strip.status()["items"]]
    assert severities == ["red", "amber"]


def test_stalled_only_when_no_lane_anywhere_can_pick(store):
    # No worker has ever registered: nothing is stalled, just nothing running.
    assert health_strip.status()["stalled"] is False

    # An online verify worker with both its lanes tripped: stalled.
    store.save_verify_worker({"host": "studio", "last_beat": _iso(seconds=5)})
    for lane in ("security", "verify"):
        worker_health.update(store, "studio", lambda r, lane=lane: worker_health.trip(
            r, lane, kind="agent-unavailable", reason="cli down"))
    assert health_strip.status()["stalled"] is True

    # One lane reopened: work can move again.
    worker_health.update(store, "studio", lambda r: worker_health.reopen(
        r, "verify", by="test"))
    assert health_strip.status()["stalled"] is False


def test_stalled_counts_a_silent_worker_as_down(store):
    store.save_fix_worker({"host": "laptop", "last_beat": _iso(hours=2)})
    assert health_strip.status()["stalled"] is True
    store.save_fix_worker({"host": "laptop", "last_beat": _iso(seconds=5)})
    assert health_strip.status()["stalled"] is False
