"""schema_gate: a machine whose code is behind the store's schema pauses its
workers, and every machine can tell which workers were left behind."""
from __future__ import annotations

import pytest

from pipeline import schema, settings
from pipeline import store as S
from pipeline.storekit import now as _now
from prospector_app.backend import data, schema_gate

CODE_V = schema.STORE_SCHEMA_VERSION


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(schema_gate, "_cache", None)
    monkeypatch.setattr(schema_gate, "_announced", set())
    data.refresh()
    return st


def _beat(st: S.Store, host: str) -> None:
    st.save_verify_worker({"host": host, "pid": 1, "last_beat": _now(),
                           "current_pr": None, "autohunt": False})


def test_a_heartbeat_records_the_schema_its_code_writes(store):
    _beat(store, "studio")
    assert store.load_verify_worker()["hosts"]["studio"]["schema"] == CODE_V


def test_code_level_with_the_store_pauses_nothing(store):
    store.save_pr({"pr": 1, "meta": {"title": "t", "state": "open", "head_sha": "a" * 40}})
    assert schema_gate.store_version() == CODE_V
    assert schema_gate.paused("verify-worker") is False


def test_code_behind_the_store_pauses_and_says_so_once(store, monkeypatch, capsys):
    monkeypatch.setattr(schema_gate, "store_version", lambda: CODE_V + 1)
    assert schema_gate.paused("verify-worker") is True
    assert schema_gate.paused("verify-worker") is True
    out = capsys.readouterr().out
    assert out.count("[verify-worker] paused") == 1
    assert f"v{CODE_V + 1}" in out and f"v{CODE_V}" in out


def test_a_worker_whose_last_beat_predates_the_bump_reads_outdated(store, monkeypatch):
    monkeypatch.setattr(settings, "worker_id", lambda: "laptop")
    _beat(store, "studio")
    _beat(store, "laptop")
    regs = store.load_worker_registries()
    assert schema_gate.outdated_hosts(regs, CODE_V, "laptop") == {}
    # The store moved on; the laptop runs the new code, the studio's last beat
    # still carries the old version.
    monkeypatch.setattr(schema, "STORE_SCHEMA_VERSION", CODE_V + 1)
    assert schema_gate.outdated_hosts(regs, CODE_V + 1, "laptop") == {"studio": CODE_V}


def test_this_machine_reads_outdated_by_its_own_code(store):
    regs = store.load_worker_registries()
    assert schema_gate.outdated_hosts(regs, CODE_V + 1, "me") == {"me": CODE_V}


def test_a_beat_with_no_recorded_schema_is_not_judged(store):
    store._merge_beat("verify_worker", {}, {"host": "old", "last_beat": _now()})
    regs = store.load_worker_registries()
    regs["verify_worker"]["hosts"]["old"].pop("schema")
    assert schema_gate.outdated_hosts(regs, CODE_V + 1, "me") == {"me": CODE_V}
