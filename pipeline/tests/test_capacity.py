"""The capacity policy: account identity, stream readings, the time-of-day cap,
weekly pacing, and the gate's decision."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from pipeline import capacity
from pipeline.store import Store

UTC = timezone.utc
LA = "America/Los_Angeles"
MAX = capacity.Account(key="k", billing="subscription", plan="max", label="pa…@example.com · Max")
# The real account lookup, held before the suite-wide stub replaces it.
_REAL_ACCOUNT = capacity.account
API = capacity.Account(key="a", billing="api", plan=None, label="API key")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture(autouse=True)
def _fresh_probe_memory(monkeypatch):
    monkeypatch.setattr(capacity, "_probe_failed", {})


@pytest.fixture(autouse=True)
def _machine_in_los_angeles(monkeypatch):
    """The default policy's zone is the machine's; these tests read times in LA."""
    monkeypatch.setattr(capacity, "_local_zone", lambda: LA)


def _event(five: float, seven: float, *, status: str = "allowed",
           five_reset: int = 1790895600, seven_reset: int = 1791439200) -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "resetsAt": five_reset, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": five_reset},
                           "seven_day": {"utilization": seven, "resetsAt": seven_reset}}}}


def _reading(five: float, seven: float, at: datetime, *, week_start: datetime | None = None,
             five_reset: datetime | None = None) -> capacity.Reading:
    week_start = week_start or at - timedelta(days=3)
    return capacity.Reading(
        five_hour=capacity.Window(five, five_reset or at + timedelta(hours=2)),
        seven_day=capacity.Window(seven, week_start + timedelta(days=7)),
        status="allowed", at=at, by="test")


def _policy(**over) -> capacity.Policy:
    base = dict(timezone=LA, day_start=time(8), day_end=time(23), day_cap=0.5, night_cap=0.9,
                weekly_pacing=True, daily_budget_usd=None, saved=True)
    base.update(over)
    return capacity.Policy(**base)


def _save_policy(store: Store, acct: capacity.Account, p: capacity.Policy) -> None:
    store.save_ai_account(acct.key, {"label": acct.label, "billing": acct.billing,
                                     "policy": capacity.policy_to_dict(p)})


def _la(hour: int, minute: int = 0, day: int = 1) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(LA)).astimezone(UTC)


# --- accounts ---------------------------------------------------------------

def test_a_max_login_is_a_subscription_with_a_masked_label():
    acct = capacity.account_from_status(
        {"loggedIn": True, "authMethod": "claude.ai", "email": "pat.tester@example.com",
         "orgId": "7b8a", "subscriptionType": "max"}, "Pat-Laptop")
    assert acct is not None
    assert (acct.billing, acct.plan, acct.label) == ("subscription", "max", "pa…@example.com · Max")
    assert "pat.tester" not in acct.key


def test_two_machines_on_one_account_share_a_key():
    status = {"loggedIn": True, "authMethod": "claude.ai", "email": "a@b.com", "orgId": "o"}
    assert (capacity.account_from_status(status, "one").key
            == capacity.account_from_status(status, "two").key)


def test_an_unidentifiable_api_login_is_keyed_to_its_machine():
    status = {"loggedIn": True, "authMethod": "api_key"}
    one = capacity.account_from_status(status, "one")
    assert one is not None and one.billing == "api" and one.label == "API key"
    assert one.key != capacity.account_from_status(status, "two").key


def test_a_logged_out_cli_has_no_account():
    assert capacity.account_from_status({"loggedIn": False}, "one") is None


# --- readings ---------------------------------------------------------------

def test_a_rate_limit_event_becomes_a_reading():
    at = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    r = capacity.parse_rate_limit(_event(0.61, 0.18), "B-Macbook", at)
    assert r is not None and r.status == "allowed" and r.by == "B-Macbook"
    assert r.five_hour == capacity.Window(0.61, datetime.fromtimestamp(1790895600, UTC))
    assert r.seven_day is not None and r.seven_day.utilization == 0.18


def test_an_event_without_windows_keeps_its_status_only():
    r = capacity.parse_rate_limit({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "resetsAt": 1790895600}}, "x", datetime.now(UTC))
    assert r is not None and r.five_hour is None and r.status == "allowed_warning"


def test_a_rejected_event_names_its_reset_and_window():
    event = _event(1.0, 0.4, status="rejected")
    assert capacity.rejection(event) == (datetime.fromtimestamp(1790895600, UTC), "five_hour")
    assert capacity.rejection(_event(0.2, 0.1)) is None


def test_a_reading_round_trips_through_its_stored_form():
    r = _reading(0.4, 0.2, datetime(2026, 10, 1, 12, tzinfo=UTC))
    assert capacity.reading_from_dict(capacity.reading_to_dict(r)) == r


# --- the cap in effect ------------------------------------------------------

@pytest.mark.parametrize("local, cap", [((7, 59), 0.9), ((8, 0), 0.5), ((22, 59), 0.5),
                                        ((23, 0), 0.9), ((3, 0), 0.9)])
def test_the_cap_follows_the_policys_own_zone(local, cap):
    assert capacity.cap_now(_policy(), _la(*local))[0] == cap


def test_the_next_boundary_is_the_next_policy_edge():
    assert capacity.cap_now(_policy(), _la(20))[1] == _la(23)
    assert capacity.cap_now(_policy(), _la(23, 30))[1] == _la(8, day=2)


def test_a_day_window_that_spans_midnight():
    p = _policy(day_start=time(22), day_end=time(6))
    assert capacity.cap_now(p, _la(23))[0] == 0.5
    assert capacity.cap_now(p, _la(5))[0] == 0.5
    assert capacity.cap_now(p, _la(12))[0] == 0.9


# --- weekly pacing ----------------------------------------------------------

def test_the_pacing_line_tracks_the_elapsed_share_of_the_week():
    start = datetime(2026, 9, 28, tzinfo=UTC)
    week = capacity.Window(0.0, start + timedelta(days=7))
    assert capacity.pacing_line(week, start) == pytest.approx(0.05)
    assert capacity.pacing_line(week, start + timedelta(days=3.5)) == pytest.approx(0.55)
    assert capacity.pacing_line(week, start + timedelta(days=7)) == pytest.approx(1.05)


# --- the gate ---------------------------------------------------------------

def test_an_unknown_account_is_paused(store):
    d = capacity.check(store, None, now=_la(12))
    assert not d.allowed and "unknown" in d.reason


def test_under_the_day_cap_is_allowed(store):
    capacity.record_reading(store, MAX, _reading(0.3, 0.2, _la(12)))
    assert capacity.check(store, MAX, now=_la(12, 1)).allowed


def test_at_the_day_cap_pauses_until_the_earlier_of_reset_and_boundary(store):
    capacity.record_reading(store, MAX, _reading(0.5, 0.2, _la(21), five_reset=_la(23, 30)))
    d = capacity.check(store, MAX, now=_la(21, 1))
    assert not d.allowed and d.retry_at == _la(23)


def test_the_same_window_is_allowed_overnight(store):
    capacity.record_reading(store, MAX, _reading(0.6, 0.2, _la(23, 30)))
    assert capacity.check(store, MAX, now=_la(23, 31)).allowed


def test_weekly_use_ahead_of_pace_pauses_until_it_catches_up(store):
    week_start = _la(12) - timedelta(days=2)  # 2/7 ≈ 0.286 elapsed, line ≈ 0.336
    capacity.record_reading(store, MAX, _reading(0.1, 0.40, _la(12), week_start=week_start))
    d = capacity.check(store, MAX, now=_la(12, 1))
    assert not d.allowed and "pace" in d.reason
    assert d.retry_at == week_start + timedelta(days=7 * (0.40 - capacity.WEEKLY_SLACK))


def test_pacing_off_ignores_the_week(store):
    _save_policy(store, MAX, _policy(weekly_pacing=False))
    capacity.record_reading(store, MAX, _reading(0.1, 0.9, _la(12)))
    assert capacity.check(store, MAX, now=_la(12, 1)).allowed


def test_a_pause_holds_until_it_ends(store):
    capacity.record_reading(store, MAX, _reading(0.1, 0.1, _la(12)))
    capacity.record_pause(store, MAX, until=_la(15), reason="usage limit reached", now=_la(12))
    d = capacity.check(store, MAX, now=_la(12, 1))
    assert not d.allowed and d.retry_at == _la(15)
    assert capacity.check(store, MAX, now=_la(15, 1), stale_ok=True).allowed


def test_a_stale_reading_is_refreshed_by_the_probe_once(store):
    capacity.record_reading(store, MAX, _reading(0.9, 0.2, _la(10)))
    calls: list[int] = []

    def probe() -> capacity.Reading:
        calls.append(1)
        return _reading(0.2, 0.2, _la(12))
    assert capacity.check(store, MAX, now=_la(12), probe=probe).allowed
    assert calls == [1]


def test_no_reading_and_no_probe_answer_pauses(store):
    d = capacity.check(store, MAX, now=_la(12), probe=lambda: None)
    assert not d.allowed and "reading" in d.reason


def test_a_stale_reading_is_used_only_when_asked(store):
    capacity.record_reading(store, MAX, _reading(0.2, 0.2, _la(10)))
    assert not capacity.check(store, MAX, now=_la(12)).allowed
    assert capacity.check(store, MAX, now=_la(12), stale_ok=True).allowed


def test_an_api_key_without_a_budget_runs_nothing(store):
    d = capacity.check(store, API, now=_la(12))
    assert not d.allowed and "budget" in d.reason


def test_an_api_key_runs_until_its_daily_budget_is_spent(store):
    _save_policy(store, API, _policy(daily_budget_usd=5.0))
    store.append_agent_run({"phase": "agent:run", "account": "a", "unattended": True,
                            "cost_usd": 4.0, "started": _la(9).isoformat(),
                            "finished": _la(9).isoformat(), "ts": _la(9).isoformat()})
    assert capacity.check(store, API, now=_la(12)).allowed
    store.append_agent_run({"phase": "agent:run", "account": "a", "unattended": True,
                            "cost_usd": 1.0, "started": _la(10).isoformat(),
                            "finished": _la(10).isoformat(), "ts": _la(10).isoformat()})
    d = capacity.check(store, API, now=_la(12))
    assert not d.allowed and d.retry_at == _la(0, day=2)


# --- policy -----------------------------------------------------------------

def test_an_account_without_a_saved_policy_runs_on_the_defaults(store):
    p = capacity.policy(store, MAX)
    assert not p.saved and (p.day_cap, p.night_cap, p.day_start, p.day_end) == (
        0.5, 0.9, time(8), time(23))


def test_a_saved_policy_is_read_back(store):
    _save_policy(store, MAX, _policy(day_cap=0.3))
    p = capacity.policy(store, MAX)
    assert p.saved and p.day_cap == 0.3


@pytest.mark.parametrize("bad", [{"day_cap": 0}, {"day_cap": 1.5}, {"night_cap": -0.1},
                                 {"day_start": "25:00"}, {"timezone": "Mars/Base"},
                                 {"day_start": "08:00", "day_end": "08:00"},
                                 {"daily_budget_usd": -1}])
def test_invalid_policy_input_is_refused(bad):
    raw = {"timezone": LA, "day_start": "08:00", "day_end": "23:00", "day_cap": 0.5,
           "night_cap": 0.9, "weekly_pacing": True, "daily_budget_usd": None, **bad}
    with pytest.raises(ValueError):
        capacity.validate_policy(raw, "subscription")


# --- the unattended mark ----------------------------------------------------

def test_the_unattended_mark_is_scoped(monkeypatch):
    monkeypatch.delenv(capacity.UNATTENDED_ENV, raising=False)
    assert capacity.current_lane() is None
    with capacity.unattended("fix"):
        assert capacity.current_lane() == "fix"
    assert capacity.current_lane() is None
    monkeypatch.setenv(capacity.UNATTENDED_ENV, "pipeline")
    assert capacity.current_lane() == "pipeline"


def test_an_attended_block_overrides_the_environment(monkeypatch):
    monkeypatch.setenv(capacity.UNATTENDED_ENV, "pipeline")
    with capacity.attended():
        assert capacity.current_lane() is None
    assert capacity.current_lane() == "pipeline"


# --- review fixes -----------------------------------------------------------

def test_a_short_email_name_is_never_shown_whole():
    acct = capacity.account_from_status(
        {"loggedIn": True, "authMethod": "claude.ai", "email": "jo@acme.com"}, "m")
    assert acct is not None and acct.label == "…@acme.com"


def test_a_window_past_its_reset_has_started_over(store):
    capacity.record_reading(store, MAX, _reading(0.95, 0.2, _la(12), five_reset=_la(12, 30)))
    assert capacity.check(store, MAX, now=_la(12, 31), stale_ok=True).allowed


def test_a_week_past_its_reset_has_started_over(store):
    week_start = _la(12) - timedelta(days=7, hours=1)
    capacity.record_reading(store, MAX, _reading(0.1, 0.9, _la(12), week_start=week_start))
    assert capacity.check(store, MAX, now=_la(12, 1)).allowed


def test_a_failed_probe_is_not_retried_for_a_while(store):
    calls: list[int] = []

    def probe() -> None:
        calls.append(1)
    capacity.check(store, MAX, now=_la(12), probe=probe)
    capacity.check(store, MAX, now=_la(12, 5), probe=probe)
    assert calls == [1]
    capacity.check(store, MAX, now=_la(12, 11), probe=probe)
    assert calls == [1, 1]


def test_over_the_next_cap_too_the_retry_is_the_reset(store):
    capacity.record_reading(store, MAX, _reading(0.95, 0.2, _la(22), five_reset=_la(23, 30)))
    d = capacity.check(store, MAX, now=_la(22, 1))
    assert not d.allowed and d.retry_at == _la(23, 30)


def test_a_shorter_pause_never_cuts_a_longer_one(store):
    capacity.record_pause(store, MAX, until=_la(20), reason="weekly", now=_la(12))
    capacity.record_pause(store, MAX, until=_la(13), reason="five-hour", now=_la(12, 1))
    assert store.load_capacity("k")["pause"]["reason"] == "weekly"


def test_an_api_policy_may_leave_out_the_caps():
    p = capacity.validate_policy({"timezone": LA, "day_start": "08:00", "day_end": "23:00",
                                  "daily_budget_usd": 5}, "api")
    assert p.daily_budget_usd == 5.0


def test_the_metering_lane_follows_the_batch_lane_then_the_mark(monkeypatch):
    monkeypatch.delenv(capacity.UNATTENDED_ENV, raising=False)
    monkeypatch.delenv(capacity.METER_ENV, raising=False)
    assert capacity.meter_lane() is None
    with capacity.metered("fix"):
        assert capacity.meter_lane() == "fix" and capacity.current_lane() is None
    monkeypatch.setenv(capacity.METER_ENV, "security")
    assert capacity.meter_lane() == "security"
    with capacity.unattended("pipeline"):
        assert capacity.meter_lane() == "pipeline"


def test_the_account_is_kept_and_a_missing_one_is_retried_later(monkeypatch):
    reads: list[int] = []
    answers = iter([None, MAX])
    monkeypatch.setattr(capacity, "_read_account", lambda: reads.append(1) or next(answers))
    monkeypatch.setattr(capacity, "_account_state", None)
    now = [1000.0]
    monkeypatch.setattr(capacity, "_monotonic", lambda: now[0])
    assert _REAL_ACCOUNT() is None and _REAL_ACCOUNT() is None and reads == [1]
    now[0] += capacity.NO_ACCOUNT_TTL_SECONDS
    assert _REAL_ACCOUNT() == MAX and reads == [1, 1]
    now[0] += 60
    assert _REAL_ACCOUNT() == MAX and reads == [1, 1]
