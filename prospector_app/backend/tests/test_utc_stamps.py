"""Stamps the app writes are UTC instants whatever the process timezone."""
from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from pipeline import schema, storekit
from prospector_app.backend import agent_memory, responses, training


@pytest.fixture(autouse=True)
def off_utc_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    zone = "Etc/GMT+12" if datetime.now(timezone.utc).hour < 12 else "Etc/GMT-14"
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    if date.today() == datetime.now(timezone.utc).date():
        pytest.skip("local date matches UTC")
    yield
    monkeypatch.undo()
    time.tzset()


def _is_now(stamp: object) -> bool:
    age = storekit.seconds_since(stamp if isinstance(stamp, str) else None)
    return age is not None and abs(age) < 60


def test_agent_memory_stamps_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = create_engine("sqlite:///:memory:")
    schema.METADATA.create_all(eng)
    monkeypatch.setattr(agent_memory, "_TEST_ENGINE", eng)
    assert _is_now(agent_memory.add("a note")["at"])


def test_training_capture_stamps_utc(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path}/store.db")
    schema.METADATA.create_all(eng)
    monkeypatch.setattr(training, "_TEST_ENGINE", eng)
    monkeypatch.setattr(training, "_features", lambda pr: {})
    assert _is_now(training.capture(1, "MERGE")["at"])


def _replied(acted: datetime, reply: datetime) -> bool:
    sig = responses.classify(acted.isoformat(), "close", state="closed",
                             comments=[{"author": {"login": "someone", "__typename": "User"},
                                        "createdAt": reply.isoformat() + "Z", "bodyText": "why?"}],
                             reopens=[], commit_dates=[])
    return bool(sig and sig["replied"])


def test_responses_read_a_naive_action_stamp_as_utc() -> None:
    acted = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    assert _replied(acted, acted + timedelta(hours=1))
    assert not _replied(acted, acted - timedelta(hours=1))
