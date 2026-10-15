"""This machine's lanes and periodic-loop passes, published to its row."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline import store as S
from prospector_app.backend import data, worker_loops

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_WORKER_ID", "mac-1")
    monkeypatch.setattr(worker_loops, "_passes", {})
    return st


def test_lanes_on_reads_the_flags(monkeypatch):
    for k in ("TRIAGE_VERIFY_WORKER", "TRIAGE_VERIFY_AUTOHUNT", "TRIAGE_FIX_WORKER",
              "TRIAGE_ISSUE_FIX_WORKER", "TRIAGE_CLUSTER_WORKER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TRIAGE_VERIFY_WORKER", "1")
    monkeypatch.setenv("TRIAGE_VERIFY_AUTOHUNT", "1")
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "off")
    on = worker_loops.lanes_on()
    assert on["verify"] is True and on["security"] is True
    assert on["autofix"] is False and on["cluster"] is False
    assert on["design"] is False


def test_ran_stamps_the_loop_and_keeps_others(store):
    worker_loops.ran("pr-watch", 900, now=NOW)
    worker_loops.ran("threat-refresh", 600, now=NOW)
    row = store.load_worker_loops()["mac-1"]
    assert row["loops"]["pr-watch"] == {"last_at": NOW.isoformat(), "every_seconds": 900}
    assert set(row["loops"]) == {"pr-watch", "threat-refresh"}
    assert set(row["lanes"]) >= {"security", "verify", "autofix"}


def test_a_store_failure_never_breaks_the_loop(store, monkeypatch):
    def boom(_: dict) -> None:
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "save_worker_loops", boom)
    worker_loops.ran("pr-watch", 900, now=NOW)
