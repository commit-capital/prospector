"""Lane health under the capacity governor: a spent usage limit is a pause, a
service overload is not a fault, and the gate answers per lane."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import capacity, settings, worker_health
from prospector_app.backend import data, lane_health

ACCT = capacity.Account(key="k", billing="subscription", plan="max", label="x · Max")


@pytest.fixture
def store(temp_store, monkeypatch):
    from pipeline.store import Store
    st = Store()
    monkeypatch.setattr(data, "store", lambda: st)
    monkeypatch.setattr(settings, "verify_worker_enabled", lambda: True)
    monkeypatch.setattr(lane_health, "_capacity_cache", {})
    monkeypatch.setattr(lane_health, "_capacity_said", {})
    monkeypatch.setattr(lane_health, "_account_noted", [])
    return st


def _tripped(st, lane: str) -> bool:
    return worker_health.is_tripped(worker_health.load(st, settings.worker_id()), lane)


def test_a_spent_usage_limit_trips_no_lane(store):
    lane_health.trip_agent_lanes("claude exited 1: You've hit your session limit")
    assert not _tripped(store, "security") and not _tripped(store, "verify")


def test_an_auth_failure_still_trips_every_lane(store):
    lane_health.trip_agent_lanes("claude exited 1: Not logged in")
    assert _tripped(store, "security") and _tripped(store, "verify")


def test_a_service_overload_books_no_failure(store):
    for _ in range(worker_health.TRIP_AFTER + 1):
        lane_health.note_failure("security", kind="security-run",
                                 reason="claude exited 1: the service was overloaded (529)")
    assert not _tripped(store, "security")


def test_an_ordinary_failure_still_counts(store):
    for _ in range(worker_health.TRIP_AFTER):
        lane_health.note_failure("security", kind="security-run", reason="boom")
    assert _tripped(store, "security")


def test_the_gate_answers_from_the_accounts_reading(store, monkeypatch):
    monkeypatch.setattr(capacity, "account", lambda refresh=False: ACCT)
    monkeypatch.setattr(capacity, "cap_now", lambda p, now: (0.5, now + timedelta(hours=1)))
    now = datetime.now(timezone.utc)
    capacity.record_reading(store, ACCT, capacity.Reading(
        five_hour=capacity.Window(0.7, now + timedelta(hours=2)), seven_day=None,
        status="allowed", at=now, by="t"))
    assert lane_health.capacity_open("security") is False
    rec = worker_health.load(store, settings.worker_id())
    assert rec["ai_account"] == {"key": "k", "label": "x · Max", "billing": "subscription"}


def test_an_unknown_account_closes_the_gate(store):
    assert lane_health.capacity_open("fix") is False
