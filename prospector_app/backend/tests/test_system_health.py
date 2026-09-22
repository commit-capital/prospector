"""system_health: the strip names tripped lanes, silent workers, and stale
ingest with the machine and the cause, and `stalled` flips only when no lane
anywhere can pick work."""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import store as S
from pipeline import worker_health
from prospector_app.backend import data, system_health


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    monkeypatch.setattr(system_health, "_issue_runs", lambda: [])
    monkeypatch.setattr(system_health, "_alert_runs", lambda: [])
    return st


NOW = datetime.now(timezone.utc)


def iso(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def test_healthy_deployment_shows_nothing(store):
    store.save_verify_worker({"host": "studio", "last_beat": iso(seconds=5),
                              "autohunt": True})
    store.append_run({"phase": "ingest", "started": iso(minutes=30),
                      "finished": iso(minutes=20)})
    out = system_health.compute(NOW)
    assert out["items"] == [] and out["severity"] == "ok"
    assert out["stalled"] is False
    assert out["lanes"]["verify"] == {"hosts": 1, "ok": True}


def test_tripped_lanes_name_the_machine_and_cause_and_stall_the_column(store):
    store.save_fix_worker({"host": "studio", "last_beat": iso(seconds=5),
                           "autohunt": True})
    worker_health.update(store, "studio", lambda r: worker_health.trip(
        r, "fix", kind="agent-unavailable", reason="Claude auth expired"))
    out = system_health.compute(NOW)
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["severity"] == "red" and item["href"] == "/control"
    assert "1/1 lane paused on studio" in item["text"]
    assert "agent-unavailable" in item["text"] and "Claude auth expired" in item["text"]
    assert out["severity"] == "red"
    # The only registered lane cannot pick work, so Home's column is stalled.
    assert out["stalled"] is True and out["lanes"]["fix"] == {"hosts": 1, "ok": False}


def test_offline_worker_supersedes_its_trip_detail(store):
    store.save_verify_worker({"host": "studio", "last_beat": iso(hours=5),
                              "autohunt": True})
    worker_health.update(store, "studio", lambda r: worker_health.trip(
        r, "verify", kind="sandbox", reason="docker down"))
    out = system_health.compute(NOW)
    assert len(out["items"]) == 1
    assert "worker studio offline" in out["items"][0]["text"]
    assert "no heartbeat for 5h" in out["items"][0]["text"]
    assert out["stalled"] is True


def test_stale_ingest_turns_amber_then_red_and_never_run_stays_silent(store):
    store.save_verify_worker({"host": "studio", "last_beat": iso(seconds=5),
                              "autohunt": True})
    assert system_health.compute(NOW)["items"] == []  # no ingest on record
    store.append_run({"phase": "ingest", "started": iso(hours=31),
                      "finished": iso(hours=30)})
    out = system_health.compute(NOW)
    assert [i["severity"] for i in out["items"]] == ["amber"]
    assert out["items"][0]["text"] == "PR ingest 30h old"
    assert out["severity"] == "amber" and out["stalled"] is False
    # A later run of another phase does not stand in for ingest.
    store.append_run({"phase": "verify:run", "started": iso(hours=1),
                      "finished": iso(hours=1)})
    out = system_health.compute(NOW + timedelta(days=19))
    assert out["items"][0]["severity"] == "red"
    assert out["items"][0]["text"] == "PR ingest 20d old"


def test_issue_and_alert_ingest_read_their_own_ledgers(store, monkeypatch):
    from pipeline import storekit
    old = storekit.PhaseRun(phase="ingest", started=iso(days=4),
                            finished=iso(days=4), raw={})
    monkeypatch.setattr(system_health, "_issue_runs", lambda: [old])
    alert = storekit.PhaseRun(phase="alert-ingest", started=iso(hours=26),
                              finished=iso(hours=26), raw={})
    monkeypatch.setattr(system_health, "_alert_runs", lambda: [alert])
    texts = {i["text"]: i["severity"] for i in system_health.compute(NOW)["items"]}
    assert texts == {"issue ingest 4d old": "red", "alert ingest 26h old": "amber"}


def test_stalled_only_when_every_represented_lane_is_down(store):
    store.save_verify_worker({"host": "laptop", "last_beat": iso(seconds=5),
                              "autohunt": True})
    store.save_fix_worker({"host": "studio", "last_beat": iso(hours=5),
                           "autohunt": True})
    out = system_health.compute(NOW)
    assert out["stalled"] is False  # verify and security still pick work
    assert out["lanes"]["fix"]["ok"] is False
    assert len(out["items"]) == 1  # the offline fix worker still makes the strip


def test_no_worker_ever_registered_is_not_stalled(store):
    out = system_health.compute(NOW)
    assert out["stalled"] is False and out["items"] == []
