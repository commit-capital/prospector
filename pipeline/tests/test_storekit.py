"""storekit: the generic validated-store + freshness primitives shared by the PR
and issue stores."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Connection, create_engine
from sqlalchemy.exc import DBAPIError

from pipeline import storekit


def _disconnect_error() -> DBAPIError:
    """A DBAPIError flagged the way SQLAlchemy flags a mid-query connection
    drop (e.g. 'SSL connection has been closed unexpectedly')."""
    err = DBAPIError("SELECT 1", None, Exception("SSL connection has been closed unexpectedly"))
    err.connection_invalidated = True
    return err


def test_read_retrying_retries_once_on_disconnect():
    eng = create_engine("sqlite://")
    calls: list[int] = []

    def fn(_conn: Connection) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise _disconnect_error()
        return "ok"

    assert storekit.read_retrying(eng, fn) == "ok"
    assert len(calls) == 2


def test_read_retrying_propagates_non_disconnect_without_retry():
    eng = create_engine("sqlite://")
    calls: list[int] = []

    def fn(_conn: Connection) -> str:
        calls.append(1)
        raise DBAPIError("SELECT 1", None, Exception("syntax error"))  # connection_invalidated stays False

    with pytest.raises(DBAPIError):
        storekit.read_retrying(eng, fn)
    assert len(calls) == 1  # no retry on a non-disconnect error


def test_read_retrying_reraises_when_retry_also_disconnects():
    eng = create_engine("sqlite://")
    calls: list[int] = []

    def fn(_conn: Connection) -> str:
        calls.append(1)
        raise _disconnect_error()

    with pytest.raises(DBAPIError):
        storekit.read_retrying(eng, fn)
    assert len(calls) == 2  # retried exactly once, then gave up


def test_bound_read_retrying_rebinds_and_retries_once_on_disconnect():
    eng = create_engine("sqlite://")
    seen: list[Connection] = []

    def fn(conn: Connection) -> str:
        seen.append(conn)
        if len(seen) == 1:
            raise _disconnect_error()
        return "ok"

    with storekit.bound_session(eng) as conn:
        assert storekit.bound_read_retrying(eng, conn, fn) == "ok"
        assert len(seen) == 2
        assert seen[1] is not seen[0]
        assert seen[0].closed  # the severed connection is discarded
        # the rest of the block runs on the replacement
        assert storekit._bound_conn(eng) is seen[1]


def test_bound_read_retrying_propagates_non_disconnect_without_rebinding():
    eng = create_engine("sqlite://")
    calls: list[int] = []

    def fn(_conn: Connection) -> str:
        calls.append(1)
        raise DBAPIError("SELECT 1", None, Exception("syntax error"))

    with storekit.bound_session(eng) as conn:
        with pytest.raises(DBAPIError):
            storekit.bound_read_retrying(eng, conn, fn)
        assert len(calls) == 1
        assert storekit._bound_conn(eng) is conn


def test_bound_read_retrying_reraises_when_the_replacement_also_disconnects():
    eng = create_engine("sqlite://")
    calls: list[int] = []

    def fn(_conn: Connection) -> str:
        calls.append(1)
        raise _disconnect_error()

    with storekit.bound_session(eng) as conn:
        with pytest.raises(DBAPIError):
            storekit.bound_read_retrying(eng, conn, fn)
        assert len(calls) == 2  # retried exactly once, then gave up


def test_bound_session_closes_the_connection_a_rebind_left_bound():
    """The block's exit closes whatever is bound at that moment. Closing the
    original by name would leak the replacement a rebind installed."""
    eng = create_engine("sqlite://")
    calls: list[int] = []

    def fn(_conn: Connection) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise _disconnect_error()
        return "ok"

    with storekit.bound_session(eng) as conn:
        storekit.bound_read_retrying(eng, conn, fn)
        replacement = storekit._bound_conn(eng)
    assert replacement is not None
    assert replacement.closed
    assert storekit._bound_conn(eng) is None


def test_stamp_sets_checked_at_and_token():
    rec: dict = {"meta": {"updated_at": "T"}}
    storekit.stamp(rec, "analysis", {"disposition": "x"}, "against_updated_at", "T")
    assert rec["analysis"]["disposition"] == "x"
    assert rec["analysis"]["against_updated_at"] == "T"
    assert "checked_at" in rec["analysis"]


def test_stamp_no_token_field_omits_token():
    rec: dict = {}
    storekit.stamp(rec, "meta", {"title": "t"}, None, None)
    assert "against_updated_at" not in rec["meta"]
    assert rec["meta"]["title"] == "t"


def test_is_current_core_token_match():
    sec = {"against_updated_at": "T", "checked_at": "2026-06-23T00:00:00+00:00"}
    assert storekit.is_current_core(sec, "against_updated_at", "T")
    assert not storekit.is_current_core(sec, "against_updated_at", "OTHER")


def test_is_current_core_no_token_field_skips_token_check():
    sec = {"checked_at": "2026-06-23T00:00:00+00:00"}
    assert storekit.is_current_core(sec, None, None)


def test_is_current_core_missing_is_false():
    assert not storekit.is_current_core(None, "against_updated_at", "T")


def test_is_current_core_schema_version():
    sec = {"against_updated_at": "T", "schema_version": 1}
    assert storekit.is_current_core(sec, "against_updated_at", "T", want_version=1)
    assert not storekit.is_current_core(sec, "against_updated_at", "T", want_version=2)


def test_is_current_core_max_age():
    sec = {"against_updated_at": "T", "checked_at": "2026-06-01T00:00:00+00:00"}
    assert storekit.is_current_core(sec, "against_updated_at", "T", max_age_days=30, today="2026-06-10")
    assert not storekit.is_current_core(sec, "against_updated_at", "T", max_age_days=5, today="2026-06-10")


@pytest.fixture
def local_tz(monkeypatch):
    """Run a test under a chosen local timezone, restoring the real one after."""
    def _set(tz: str) -> None:
        monkeypatch.setenv("TZ", tz)
        time.tzset()
    yield _set
    monkeypatch.undo()
    time.tzset()


# Kiritimati is UTC+14 and Midway UTC-11 — a 25-hour spread, so whatever the
# real UTC time, these three do not all share one calendar date. checked_at
# values are UTC (`now`), so an age measured against them is a property of the
# instants alone: the machine's timezone must not move it.
@pytest.mark.parametrize("tz", ["UTC", "Pacific/Kiritimati", "Pacific/Midway"])
def test_age_days_is_independent_of_the_local_timezone(tz, local_tz):
    local_tz(tz)
    checked_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    assert storekit._age_days(checked_at, None) == 10


@pytest.mark.parametrize("tz", ["UTC", "Pacific/Kiritimati", "Pacific/Midway"])
def test_max_age_window_is_independent_of_the_local_timezone(tz, local_tz):
    """The gate the age feeds: 8 days old against a 7-day window is stale in
    every timezone."""
    local_tz(tz)
    checked_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    sec = {"against_updated_at": "T", "checked_at": checked_at}
    assert not storekit.is_current_core(sec, "against_updated_at", "T", max_age_days=7)
    assert storekit.currency_failure_core(
        sec, "against_updated_at", "T", max_age_days=7) == "8d old, outside the 7d window"
