"""Operator item claims (#323): the shared claims registry, the claimed
filter, and the machine stamp on activity events."""
from __future__ import annotations

import threading
import time

import pytest

from pipeline import schema
from pipeline import store as S
from pipeline import storekit
from prospector_app.backend import activity
from prospector_app.backend import claims
from prospector_app.backend import data
from prospector_app.backend import filters


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("PROSPECTOR_OPERATOR", "Alice")
    activity.operator.cache_clear()
    monkeypatch.setenv("TRIAGE_WORKER_ID", "mac-1")
    yield st
    activity.operator.cache_clear()


def test_claim_release_roundtrip(store):
    assert claims.for_item("pr", 7) is None
    rec = claims.claim("pr", 7)
    assert rec["by"] == "Alice" and rec["machine"] == "mac-1" and rec["at"]
    assert claims.for_item("pr", 7) == rec
    assert store.load_claims()["items"] == {"pr:7": rec}
    claims.release("pr", 7)
    assert claims.for_item("pr", 7) is None
    assert store.load_claims()["items"] == {}


def test_pr_and_issue_claims_are_distinct(store):
    claims.claim("pr", 7)
    assert claims.for_item("issue", 7) is None
    claims.claim("issue", 7)
    assert set(store.load_claims()["items"]) == {"pr:7", "issue:7"}


def test_unknown_kind_refused(store):
    with pytest.raises(ValueError):
        claims.claim("cluster", 1)


def test_load_fails_soft_to_unclaimed(monkeypatch):
    def boom():
        raise RuntimeError("store down")
    monkeypatch.setattr(data, "store", boom)
    assert claims.load() == {}


def _expire_cache() -> None:
    """Let the next load start a background re-read."""
    claims._snapshot._last_check = 0.0


def _wait_for(predicate) -> bool:
    for _ in range(200):
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _gate_first_read(store, monkeypatch) -> threading.Event:
    """Hold the next load_claims after it has read the store, until the
    returned event is set; later reads pass straight through."""
    gate = threading.Event()
    real = store.load_claims
    held = []

    def gated():
        result = real()
        if not held:
            held.append(True)
            gate.wait(2)
        return result
    monkeypatch.setattr(store, "load_claims", gated)
    return gate


def test_expired_claims_are_served_while_a_background_read_runs(store, monkeypatch):
    assert claims.for_item("pr", 8) is None
    store.save_claims({"items": {"pr:8": {"by": "Bob", "machine": "mac-2", "at": "t"}}})
    _expire_cache()
    gate = _gate_first_read(store, monkeypatch)

    assert claims.for_item("pr", 8) is None  # the kept read, without waiting on the store
    gate.set()
    assert _wait_for(lambda: claims.for_item("pr", 8) is not None)


def test_a_claim_made_during_a_background_read_shows_on_the_next_call(store, monkeypatch):
    assert claims.for_item("pr", 9) is None
    _expire_cache()
    gate = _gate_first_read(store, monkeypatch)
    claims.load()  # starts the background read, which holds a copy without pr:9

    made: dict[str, dict] = {}
    claimer = threading.Thread(target=lambda: made.update(rec=claims.claim("pr", 9)))
    claimer.start()
    gate.set()
    claimer.join(2)

    assert claims.for_item("pr", 9) == made["rec"]


def test_a_failed_re_read_keeps_the_last_claims(store, monkeypatch):
    rec = claims.claim("pr", 7)
    assert claims.for_item("pr", 7) == rec

    def boom():
        raise RuntimeError("store down")
    monkeypatch.setattr(store, "load_claims", boom)
    claims._snapshot.refresh()

    assert claims.for_item("pr", 7) == rec


def test_release_by_another_operator_drops_the_claim(store, monkeypatch):
    claims.claim("pr", 9)
    monkeypatch.setenv("PROSPECTOR_OPERATOR", "Bob")
    activity.operator.cache_clear()
    claims.release("pr", 9)
    assert claims.for_item("pr", 9) is None


# ── the claimed filter ────────────────────────────────────────────────────────

def _row(claim: dict | None) -> dict:
    return {"number": 1, "title": "t", "author": "a", "claim": claim}


def test_filter_unclaimed():
    assert filters.matches(_row(None), {"claimed": "unclaimed"})
    assert not filters.matches(_row({"by": "Alice"}), {"claimed": "unclaimed"})


def test_filter_by_operator_name():
    assert filters.matches(_row({"by": "Alice"}), {"claimed": "Alice"})
    assert not filters.matches(_row({"by": "Bob"}), {"claimed": "Alice"})
    assert not filters.matches(_row(None), {"claimed": "Alice"})


# ── the machine stamp on activity events ──────────────────────────────────────

def test_record_stamps_the_machine(tmp_path, monkeypatch):
    eng = storekit.get_engine(f"sqlite:///{tmp_path}/activity.db")
    schema.METADATA.create_all(eng)
    storekit.assert_repo(eng)
    monkeypatch.setattr(activity, "_engine", lambda: eng)
    monkeypatch.setattr(activity, "operator", lambda: {"name": "T", "email": None})
    monkeypatch.setenv("TRIAGE_WORKER_ID", "mac-2")
    entry = activity.record("comment", pr=5, status="executed")
    assert entry["machine"] == "mac-2"
    assert [e.get("machine") for e in activity.recent(5)] == ["mac-2"]
