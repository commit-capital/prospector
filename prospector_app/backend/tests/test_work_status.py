"""The header's one-line answer to "what is the system doing right now":
work_status.now() merges both lanes' running queue entries with the shared
worker heartbeat registries, so a laptop shows work running on any machine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import store as S
from pipeline.storekit import now as _now
from prospector_app.backend import data
from prospector_app.backend import fix_queue
from prospector_app.backend import verify_queue
from prospector_app.backend import work_status


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    st.save_pr({"pr": 1, "meta": {"title": "fix boom", "state": "open",
                                  "head_sha": "a" * 40}})
    st.save_pr({"pr": 2, "meta": {"title": "fix bar", "state": "open",
                                  "head_sha": "b" * 40}})
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    return st


def test_an_idle_store_reports_nothing_active(store):
    s = work_status.now()
    assert s["active"] == []
    assert s["queued"] == {"verify": 0, "fix": 0}
    assert s["awaiting_review"] == 0
    assert s["workers"] == []


def test_a_running_fix_appears_with_its_progress_and_liveness(store):
    fix_queue.queue_pr(1, "update")
    store.claim_fix_request(1, host="mac-studio")
    store.save_fix_worker({"host": "mac-studio", "pid": 1, "last_beat": _now(),
                           "current_pr": 1, "autohunt": False})
    data.refresh()
    [a] = work_status.now()["active"]
    assert a["lane"] == "fix"
    assert a["pr"] == 1
    assert a["action"] == "update"
    assert a["step"] == "claimed"
    assert a["host"] == "mac-studio"
    assert a["started_at"]
    assert a["worker_online"] is True


def test_a_running_verify_with_a_dead_worker_reads_offline(store):
    # The claim named a host whose heartbeat has gone stale — the header says
    # "worker offline" instead of pretending the run is progressing.
    verify_queue.queue_pr(1)
    store.claim_verify_request(1, host="mac-studio")
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    store.save_verify_worker({"host": "mac-studio", "pid": 1, "last_beat": old,
                              "current_pr": 1, "autohunt": False})
    data.refresh()
    [a] = work_status.now()["active"]
    assert a["lane"] == "verify"
    assert a["worker_online"] is False


def test_queued_and_parked_work_is_counted(store):
    verify_queue.queue_pr(1)
    fix_queue.queue_pr(1, "update")
    store.edit_pr(2).record_fix_request("awaiting-review", "rebase",
                                        result={"patch": "diff"})
    data.refresh()
    s = work_status.now()
    assert s["queued"] == {"verify": 1, "fix": 1}
    assert s["awaiting_review"] == 1


def test_waiting_for_base_counts_as_queued_verify_work(store):
    store.edit_pr(1).record_verify_request("waiting-for-base", queued_at=_now())
    data.refresh()
    assert work_status.now()["queued"]["verify"] == 1


def test_workers_lists_every_known_host_with_liveness(store):
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    store.save_verify_worker({"host": "mac-studio", "pid": 1, "last_beat": _now(),
                              "current_pr": None, "autohunt": True})
    store.save_fix_worker({"host": "old-laptop", "pid": 2, "last_beat": old,
                           "current_pr": None, "autohunt": False})
    workers = work_status.now()["workers"]
    assert {(w["lane"], w["host"], w["online"]) for w in workers} == {
        ("verify", "mac-studio", True), ("fix", "old-laptop", False)}


def test_the_status_route_serves_the_aggregate(store):
    from fastapi.testclient import TestClient

    from prospector_app.backend import app as appmod

    r = TestClient(appmod.app).get("/api/status/now")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active"] == []
    assert "queued" in body and "workers" in body and "jobs" in body
