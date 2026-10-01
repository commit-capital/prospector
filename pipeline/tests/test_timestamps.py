from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pytest

from pipeline import review_policy, storekit


@pytest.fixture
def off_utc_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A process timezone whose calendar date differs from UTC's right now:
    UTC-12 while UTC is before noon, UTC+14 from 10:00 UTC on."""
    zone = "Etc/GMT+12" if datetime.now(timezone.utc).hour < 12 else "Etc/GMT-14"
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    if date.today() == datetime.now(timezone.utc).date():
        pytest.skip("local date matches UTC")
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("stamp", [
    "2026-10-01T12:00:00Z",
    "2026-10-01T12:00:00+00:00",
    "2026-10-01T14:00:00+02:00",
    "2026-10-01T12:00:00",
])
def test_parse_ts_reads_every_form_as_the_same_utc_instant(stamp: str) -> None:
    at = storekit.parse_ts(stamp)
    assert at == datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert at is not None and at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("stamp", [None, "", "not a time", "2026-13-01T00:00:00Z"])
def test_parse_ts_absent_or_invalid_is_none(stamp: str | None) -> None:
    assert storekit.parse_ts(stamp) is None


def test_seconds_and_hours_since() -> None:
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert storekit.seconds_since("2026-10-01T10:30:00Z", now) == 5400
    assert storekit.hours_since("2026-10-01T10:30:00Z", now) == 1.5
    assert storekit.seconds_since("2026-10-01T13:00:00Z", now) == -3600
    assert storekit.hours_since("2026-10-01T13:00:00Z", now) == 0.0
    assert storekit.hours_since("garbage", now) is None


def test_utc_day_and_midnight_follow_utc_across_an_offset_boundary() -> None:
    late_west = datetime(2026, 10, 1, 23, 30, tzinfo=timezone(timedelta(hours=-2)))
    assert storekit.utc_midnight(late_west) == datetime(2026, 10, 2, tzinfo=timezone.utc)
    assert storekit.utc_day(late_west) == "2026-10-02"
    assert storekit.utc_day(datetime(2026, 10, 1, 23, 59, 59)) == "2026-10-01"
    assert storekit.utc_day(datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)) == "2026-10-02"


@pytest.mark.usefixtures("off_utc_zone")
def test_utc_day_ignores_the_process_timezone() -> None:
    assert storekit.utc_day() == datetime.now(timezone.utc).date().isoformat()


@pytest.mark.usefixtures("off_utc_zone")
def test_review_policy_window_is_measured_from_the_utc_day() -> None:
    assert review_policy._today() == datetime.now(timezone.utc).date().isoformat()


def test_age_days_reads_an_offset_stamp_on_its_utc_date() -> None:
    assert storekit._age_days("2026-10-01T23:30:00-02:00", "2026-10-02") == 0
