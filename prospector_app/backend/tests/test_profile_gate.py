"""profile_gate: while the live workers' profiles differ, every machine's work
loops pause, and the health strip names how the machines differ."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import profile, settings
from pipeline import store as S
from pipeline.storekit import now as _now
from prospector_app.backend import data, profile_gate, system_health

OBJECTION = profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("review", "objection")))


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(profile_gate, "_cache", None)
    monkeypatch.setattr(profile_gate, "_announced", set())
    monkeypatch.setattr(settings, "worker_id", lambda: "laptop")
    data.refresh()
    return st


def _beat(st: S.Store, host: str, p: profile.RepoProfile, monkeypatch: pytest.MonkeyPatch,
          last_beat: str | None = None) -> None:
    monkeypatch.setattr(profile, "active", lambda: p)
    st.save_verify_worker({"host": host, "pid": 1, "last_beat": last_beat or _now(),
                           "current_pr": None, "autohunt": False})


def test_a_heartbeat_records_its_profile_stamp(store, monkeypatch):
    _beat(store, "studio", OBJECTION, monkeypatch)
    rec = store.load_verify_worker()["hosts"]["studio"]["profile"]
    assert rec == profile.stamp(OBJECTION)
    assert rec["fixable_gates"] == ["review", "objection"]


def test_the_stamp_tells_profiles_apart():
    assert profile.stamp(OBJECTION)["fingerprint"] != profile.stamp(profile.GENERIC)["fingerprint"]
    assert profile.stamp(profile.RepoProfile()) == profile.stamp(profile.GENERIC)


def test_the_fingerprint_covers_only_fields_set_away_from_their_defaults():
    assert profile._set_fields(profile.GENERIC) == {}
    assert profile._set_fields(OBJECTION) == {"autofix": {"fixable_gates": ["review", "objection"]}}


def test_matching_profiles_pause_nothing(store, monkeypatch):
    _beat(store, "studio", profile.GENERIC, monkeypatch)
    _beat(store, "laptop", profile.GENERIC, monkeypatch)
    assert profile_gate.drift(store.load_worker_registries()) == {}
    assert profile_gate.paused("fix-worker") is False


def test_a_live_worker_with_another_profile_pauses_this_machine(store, monkeypatch, capsys):
    _beat(store, "studio", OBJECTION, monkeypatch)
    _beat(store, "laptop", profile.GENERIC, monkeypatch)
    d = profile_gate.drift(store.load_worker_registries())
    assert set(d) == {"studio", "laptop"}
    assert profile_gate.paused("fix-worker") is True
    assert profile_gate.paused("fix-worker") is True
    assert capsys.readouterr().out.count("[fix-worker] paused") == 1


def test_an_offline_worker_is_not_judged(store, monkeypatch):
    hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    _beat(store, "studio", OBJECTION, monkeypatch, last_beat=hours_ago)
    _beat(store, "laptop", profile.GENERIC, monkeypatch)
    assert profile_gate.drift(store.load_worker_registries()) == {}


def test_a_machine_running_no_workers_is_not_judged(store, monkeypatch):
    _beat(store, "studio", OBJECTION, monkeypatch)
    monkeypatch.setattr(profile, "active", lambda: profile.GENERIC)
    assert profile_gate.drift(store.load_worker_registries()) == {}


def test_a_beat_with_no_stamp_is_not_judged(store, monkeypatch):
    _beat(store, "studio", OBJECTION, monkeypatch)
    _beat(store, "laptop", profile.GENERIC, monkeypatch)
    regs = store.load_worker_registries()
    regs["verify_worker"]["hosts"]["studio"].pop("profile")
    assert profile_gate.drift(regs) == {}


def test_drift_downs_every_lane_and_names_each_machine():
    drift = {"studio": profile.stamp(OBJECTION), "laptop": profile.stamp(profile.GENERIC)}
    out = system_health.summarize(
        [], [], [("fix", "studio"), ("fix", "laptop")], {}, None, profile_drift=drift)
    item = out["items"][0]
    assert item["kind"] == "profile-drift" and item["severity"] == "red"
    assert item["label"] == "Profiles differ across 2 machines — workers paused"
    assert "studio: " + drift["studio"]["fingerprint"] in (item["detail"] or "")
    assert "fixable gates review, objection" in (item["detail"] or "")
    assert out["workers_stalled"] is True


def test_a_machine_with_an_older_stamp_reads_as_out_of_date():
    old = {**profile.stamp(OBJECTION), "fingerprint": "08b3ab3734e7"}
    old.pop("version")
    drift = {"studio": old, "laptop": profile.stamp(OBJECTION)}
    out = system_health.summarize([], [], [("fix", "studio"), ("fix", "laptop")], {}, None,
                                  profile_drift=drift)
    assert out["items"][0]["label"] == "studio out of date — update Prospector there"


def test_drift_with_one_machine_left_after_stand_downs_is_no_drift():
    drift = {"studio": profile.stamp(OBJECTION), "laptop": profile.stamp(profile.GENERIC)}
    out = system_health.summarize([], [], [("fix", "studio")], {}, None,
                                  stood_down=frozenset({"laptop"}), profile_drift=drift)
    assert not any(i["kind"] == "profile-drift" for i in out["items"])
    assert out["workers_stalled"] is False
