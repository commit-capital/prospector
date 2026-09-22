"""escalation: a trip is ledgered, offline workers are listed for the banner,
and Resume reopens a lane."""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import settings, worker_health
from pipeline import store as S
from prospector_app.backend import data, escalation


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    return st


def test_escalate_trip_ledgers_each_trip(store):
    me = settings.worker_id()
    worker_health.update(store, me, lambda r: worker_health.trip(
        r, "fix", kind="sandbox", reason="docker down"))
    escalation.escalate_trip(["fix"])
    trips = [r for r in store.runs() if getattr(r, "phase", "") == "worker:trip"]
    assert [r.raw["stats"]["kind"] for r in trips] == ["sandbox"]


def test_offline_workers_lists_hosts_past_the_silence_bar(store):
    now = datetime.now(timezone.utc)
    dark = (now - timedelta(hours=5)).isoformat()
    fresh = (now - timedelta(seconds=10)).isoformat()
    store.save_verify_worker({"host": "studio", "last_beat": dark, "autohunt": True})
    store.save_verify_worker({"host": "laptop", "last_beat": fresh, "autohunt": True})
    assert [w["host"] for w in escalation.offline_workers(now)] == ["studio"]


def test_resume_reopens_and_ledgers(store):
    me = settings.worker_id()
    worker_health.update(store, me, lambda r: worker_health.trip(
        r, "verify", kind="pin-refresh", reason="x"))
    escalation.resume(me, "verify")
    assert not worker_health.is_tripped(worker_health.load(store, me), "verify")
    with pytest.raises(ValueError):
        escalation.resume(me, "teleport")


def test_health_status_lists_tripped_workers_first(store):
    worker_health.update(store, "b", lambda r: worker_health.record_success(r, "fix"))
    worker_health.update(store, "a", lambda r: worker_health.trip(r, "fix", kind="k", reason="r"))
    worker_health.update(store, "c", lambda r: worker_health.trip(r, "verify", kind="k", reason="r"))
    st = escalation.health_status()
    assert [h["host"] for h in st["hosts"]] == ["a", "c", "b"]
    assert st["any_tripped"] is True
    assert st["hosts"][0]["tripped"] == ["fix"]
