"""Machines stood down on purpose: the mark holds until a worker on that
machine beats again."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline import store as S
from prospector_app.backend import data, machine_mode

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _roster(**beats: str) -> dict:
    return {"local": "x", "machines": [
        {"host": h, "beats": {"verify": {"last_beat": b}}} for h, b in beats.items()]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    return st


def test_mark_holds_while_machine_is_silent(store):
    machine_mode.stand_down("lap", by="ops", note="travel", now=NOW)
    out = machine_mode.stood_down(store.load_machine_modes(), _roster(lap=_iso(30)))
    assert set(out) == {"lap"} and out["lap"]["note"] == "travel"


def test_beat_after_mark_ends_stand_down(store):
    machine_mode.stand_down("lap", by="ops", now=NOW - timedelta(hours=2))
    assert machine_mode.stood_down(store.load_machine_modes(), _roster(lap=_iso(1))) == {}


def test_host_with_no_beats_stays_stood_down(store):
    machine_mode.stand_down("gone", by="ops", now=NOW)
    assert set(machine_mode.stood_down(store.load_machine_modes(), _roster())) == {"gone"}


def test_stand_up_clears(store):
    machine_mode.stand_down("lap", by="ops", now=NOW)
    assert machine_mode.stand_up("lap") is True
    assert store.load_machine_modes() == {}


def test_endpoints(store, monkeypatch):
    from prospector_app.backend import app as app_mod
    monkeypatch.setenv("TRIAGE_REPO", "owner/name")
    client = TestClient(app_mod.app)
    r = client.post("/api/machines/lap/stand-down", json={"note": "off"})
    assert r.status_code == 200 and r.json()["stood_down"] is True
    assert "lap" in store.load_machine_modes()
    assert client.post("/api/machines/lap/stand-up").json() == {"cleared": True}


def test_stood_down_in_reads_one_registries_load(store):
    machine_mode.stand_down("lap", by="ops", now=NOW)
    store.save_fix_worker({"host": "lap", "last_beat": _iso(30)})
    assert set(machine_mode.stood_down_in(store.load_worker_registries())) == {"lap"}
    store.save_fix_worker({"host": "lap", "last_beat": (NOW + timedelta(minutes=1)).isoformat()})
    assert machine_mode.stood_down_in(store.load_worker_registries()) == {}
