"""The ONE AI capacity policy: how much of each AI account unattended agent
work may spend.

A machine's agents run under the account its Claude CLI is logged into
(`claude auth status --json`). Every headless run reports the account's live
window utilization in a `rate_limit_event`; the newest reading from any
machine on the account, and any pause after a usage-limit hit, live in the
store's `ai_capacity:<key>` row, and the account's policy in `ai_account:<key>`.
`check()` decides whether unattended work may start an agent now: under a
subscription, while the 5-hour window is below the cap in effect at this time
of day and weekly use is within its pace; under an API key, while today's
reported spend is below the daily budget. Work an operator starts is never
gated.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time as clock
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pipeline import settings
from pipeline import storekit

if TYPE_CHECKING:
    from pipeline.store import Store

# A reading older than this is refreshed before a decision rests on it.
READING_MAX_AGE = timedelta(minutes=10)
# Weekly use may run this far ahead of the elapsed share of the week.
WEEKLY_SLACK = 0.05
# How soon a decision without a reading is retried.
RETRY_UNKNOWN = timedelta(minutes=10)
# The variable that marks a process's agent work an unattended batch, naming
# its lane: every agent call in it waits on the gate.
UNATTENDED_ENV = "PROSPECTOR_UNATTENDED"
# The variable that names the lane a process's agent runs are metered under,
# for a worker's subprocess that runs an unattended item.
METER_ENV = "PROSPECTOR_AGENT_LANE"
# The failure kind a worker's ledger entry carries for an unattended item it
# stopped short of an agent because the gate was closed: a wait, not a fault.
PAUSED_KIND = "capacity-paused"
WEEK = timedelta(days=7)
FIVE_HOURS = timedelta(hours=5)
# How long a found account, and a failure to find one, are kept.
ACCOUNT_TTL_SECONDS = 3600.0
NO_ACCOUNT_TTL_SECONDS = 300.0

Billing = Literal["subscription", "api"]


@dataclass(frozen=True)
class Account:
    key: str
    billing: Billing
    plan: str | None
    label: str


@dataclass(frozen=True)
class Window:
    utilization: float
    resets_at: datetime


@dataclass(frozen=True)
class Reading:
    five_hour: Window | None
    seven_day: Window | None
    status: str
    at: datetime
    by: str


@dataclass(frozen=True)
class Policy:
    timezone: str
    day_start: time
    day_end: time
    day_cap: float
    night_cap: float
    weekly_pacing: bool
    daily_budget_usd: float | None
    saved: bool


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    retry_at: datetime | None


class CapacityPaused(RuntimeError):
    """Unattended work asked to start an agent while the gate is closed. A
    deferral, never a failure: the work waits for `decision.retry_at`."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


# --- accounts ---------------------------------------------------------------

def _mask(email: str) -> str:
    """The email with all but the first characters of its name hidden — at
    most two, and never the whole name."""
    name, _, domain = email.partition("@")
    shown = name[:min(2, max(0, len(name) - 2))]
    return f"{shown}…@{domain}" if domain else f"{shown}…"


def account_from_status(status: dict, worker_id: str) -> Account | None:
    """The account `claude auth status --json` describes, or None when the CLI
    is not logged in. Machines on one account share its key; a login that
    names neither an org nor an email is keyed to its machine alone."""
    if not status.get("loggedIn"):
        return None
    method = str(status.get("authMethod") or "")
    org = str(status.get("orgId") or "")
    email = str(status.get("email") or "")
    seed = f"{method}|{org}|{email}" if (org or email) else f"machine|{worker_id}"
    billing: Billing = "subscription" if method == "claude.ai" else "api"
    plan = str(status["subscriptionType"]) if status.get("subscriptionType") else None
    name = _mask(email) if email else ("API key" if billing == "api" else "Claude account")
    return Account(key=hashlib.sha256(seed.encode()).hexdigest()[:16], billing=billing,
                   plan=plan, label=f"{name} · {plan.title()}" if plan else name)


_account_lock = threading.Lock()
_account_state: tuple[float, Account | None] | None = None
_monotonic: Callable[[], float] = clock.monotonic


def _read_account() -> Account | None:
    try:
        out = subprocess.run([shutil.which("claude") or "claude", "auth", "status", "--json"],
                             capture_output=True, text=True, timeout=15).stdout
        status = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return account_from_status(status, settings.worker_id()) if isinstance(status, dict) else None


def account(refresh: bool = False) -> Account | None:
    """This machine's AI account, kept ACCOUNT_TTL_SECONDS so a new /login is
    noticed within the hour; None while the CLI is missing, not logged in or
    not answering, kept NO_ACCOUNT_TTL_SECONDS before asking again."""
    global _account_state
    with _account_lock:
        state = _account_state
    if state is not None and not refresh:
        at, found = state
        if _monotonic() - at < (ACCOUNT_TTL_SECONDS if found else NO_ACCOUNT_TTL_SECONDS):
            return found
    found = _read_account()
    with _account_lock:
        _account_state = (_monotonic(), found)
    return found


# --- readings ---------------------------------------------------------------

def _window(raw: object) -> Window | None:
    if not isinstance(raw, dict):
        return None
    used, reset = raw.get("utilization"), raw.get("resetsAt")
    if not isinstance(used, (int, float)) or not isinstance(reset, (int, float)):
        return None
    return Window(float(used), datetime.fromtimestamp(reset, timezone.utc))


def parse_rate_limit(event: dict, by: str, at: datetime) -> Reading | None:
    """The reading a `rate_limit_event` carries, or None for any other event."""
    info = event.get("rate_limit_info")
    if event.get("type") != "rate_limit_event" or not isinstance(info, dict):
        return None
    windows = info.get("unifiedWindows") or {}
    if not isinstance(windows, dict):
        windows = {}
    return Reading(five_hour=_window(windows.get("five_hour")),
                   seven_day=_window(windows.get("seven_day")),
                   status=str(info.get("status") or ""), at=at, by=by)


def rejection(event: dict) -> tuple[datetime | None, str | None] | None:
    """When a `rate_limit_event` refused the run: the reset time of the window
    that refused it, and that window's name. None for any event that did not."""
    info = event.get("rate_limit_info")
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return None
    reset = info.get("resetsAt")
    kind = info.get("rateLimitType")
    return (datetime.fromtimestamp(reset, timezone.utc) if isinstance(reset, (int, float)) else None,
            str(kind) if kind else None)


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def reading_to_dict(r: Reading) -> dict:
    def win(w: Window | None) -> dict | None:
        return None if w is None else {"utilization": w.utilization, "resets_at": _iso(w.resets_at)}
    return {"five_hour": win(r.five_hour), "seven_day": win(r.seven_day), "status": r.status,
            "at": _iso(r.at), "by": r.by}


def reading_from_dict(d: dict) -> Reading | None:
    at = storekit.parse_ts(d.get("at"))
    if at is None:
        return None

    def win(raw: object) -> Window | None:
        if not isinstance(raw, dict):
            return None
        reset = storekit.parse_ts(raw.get("resets_at"))
        used = raw.get("utilization")
        if reset is None or not isinstance(used, (int, float)):
            return None
        return Window(float(used), reset)
    return Reading(five_hour=win(d.get("five_hour")), seven_day=win(d.get("seven_day")),
                   status=str(d.get("status") or ""), at=at, by=str(d.get("by") or ""))


def record_reading(store: Store, acct: Account, reading: Reading) -> None:
    store.save_capacity_reading_if_newer(acct.key, reading_to_dict(reading))


_pause_generation = [0]


def pause_generation() -> int:
    """How many pauses this process has recorded — a cached decision taken
    before the latest is out of date."""
    return _pause_generation[0]


def record_pause(store: Store, acct: Account, until: datetime, reason: str,
                 now: datetime | None = None) -> None:
    """Pause the account's unattended agent work until `until`, and note the
    pause in the agent ledger. A pause still running past `until` stands."""
    at = now or datetime.now(timezone.utc)
    _pause_generation[0] += 1
    held = storekit.parse_ts((store.load_capacity(acct.key).get("pause") or {}).get("until"))
    if held is not None and held > max(until, at):
        return
    store.save_capacity_pause_if_newer(acct.key, {"at": _iso(at), "until": _iso(until),
                                                  "reason": reason})
    store.append_agent_run({"phase": "capacity:pause", "account": acct.key, "until": _iso(until),
                            "reason": reason, "started": _iso(at), "finished": _iso(at),
                            "ts": _iso(at)})


# --- policy -----------------------------------------------------------------

def _local_zone() -> str:
    """This machine's IANA time zone name, else UTC."""
    tz = os.environ.get("TZ", "")
    if tz and _known_zone(tz):
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return "UTC"
    _, sep, name = target.partition("zoneinfo/")
    return name if sep and _known_zone(name) else "UTC"


def _known_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def default_policy(billing: Billing) -> Policy:
    return Policy(timezone=_local_zone(), day_start=time(8), day_end=time(23), day_cap=0.5,
                  night_cap=0.9, weekly_pacing=True, daily_budget_usd=None, saved=False)


def _parse_time(raw: object, field: str) -> time:
    if isinstance(raw, str):
        try:
            hour, minute = (int(p) for p in raw.split(":"))
            return time(hour, minute)
        except ValueError:
            pass
    raise ValueError(f"{field} must be a time like 08:00")


def _fraction(raw: object, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 < raw <= 1:
        raise ValueError(f"{field} must be above 0 and at most 1")
    return float(raw)


def validate_policy(raw: dict, billing: Billing) -> Policy:
    """A saved policy from operator input; ValueError names the first bad field.
    An API key's policy may leave the caps out, which no decision reads."""
    zone = raw.get("timezone")
    if not isinstance(zone, str) or not _known_zone(zone):
        raise ValueError("timezone must be an IANA zone such as America/Los_Angeles")
    start, end = _parse_time(raw.get("day_start"), "day_start"), _parse_time(raw.get("day_end"), "day_end")
    if start == end:
        raise ValueError("day_start and day_end must differ")
    if billing == "api":
        raw = {"day_cap": 0.5, "night_cap": 0.9, **raw}
    budget = raw.get("daily_budget_usd")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))
                               or budget < 0):
        raise ValueError("daily_budget_usd must be a non-negative number")
    return Policy(timezone=zone, day_start=start, day_end=end,
                  day_cap=_fraction(raw.get("day_cap"), "day_cap"),
                  night_cap=_fraction(raw.get("night_cap"), "night_cap"),
                  weekly_pacing=bool(raw.get("weekly_pacing", True)),
                  daily_budget_usd=None if budget is None else float(budget), saved=True)


def policy_to_dict(p: Policy) -> dict:
    return {"timezone": p.timezone, "day_start": p.day_start.strftime("%H:%M"),
            "day_end": p.day_end.strftime("%H:%M"), "day_cap": p.day_cap,
            "night_cap": p.night_cap, "weekly_pacing": p.weekly_pacing,
            "daily_budget_usd": p.daily_budget_usd}


def policy(store: Store, acct: Account) -> Policy:
    """The account's saved policy, else the defaults."""
    raw = (store.load_ai_account(acct.key) or {}).get("policy")
    if isinstance(raw, dict):
        try:
            return validate_policy(raw, acct.billing)
        except ValueError:
            pass
    return default_policy(acct.billing)


# --- the decision -----------------------------------------------------------

def _is_day(p: Policy, t: time) -> bool:
    if p.day_start < p.day_end:
        return p.day_start <= t < p.day_end
    return t >= p.day_start or t < p.day_end


def cap_now(p: Policy, now: datetime) -> tuple[float, datetime]:
    """The 5-hour cap in effect at `now` in the policy's zone, and when the next
    day/night boundary falls (UTC)."""
    zone = ZoneInfo(p.timezone)
    local = now.astimezone(zone)
    cap = p.day_cap if _is_day(p, local.time()) else p.night_cap
    edges = [datetime.combine(day, edge, tzinfo=zone)
             for day in (local.date(), local.date() + timedelta(days=1))
             for edge in (p.day_start, p.day_end)]
    return cap, min(e for e in edges if e > local).astimezone(timezone.utc)


def pacing_line(seven_day: Window, now: datetime) -> float:
    """The weekly utilization unattended work may reach by `now`: the elapsed
    share of this account's week plus WEEKLY_SLACK."""
    start = seven_day.resets_at - WEEK
    elapsed = min(max((now - start) / WEEK, 0.0), 1.0)
    return elapsed + WEEKLY_SLACK


def local_midnights(p: Policy, now: datetime) -> tuple[datetime, datetime]:
    """The start of `now`'s day in the policy's zone and the start of the next (UTC)."""
    zone = ZoneInfo(p.timezone)
    today: date = now.astimezone(zone).date()
    start = datetime.combine(today, time(0), tzinfo=zone)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _current(window: Window | None, now: datetime, period: timedelta) -> Window | None:
    """`window` as of `now`: a window whose reset has passed has started over,
    empty, and resets one period later."""
    if window is None or window.resets_at > now:
        return window
    periods = (now - window.resets_at) // period + 1
    return Window(0.0, window.resets_at + period * periods)


# When a probe last came back without a reading, per account; the next probe
# waits RETRY_UNKNOWN after it.
_probe_failed: dict[str, datetime] = {}


def check(store: Store, acct: Account | None, now: datetime | None = None,
          probe: Callable[[], Reading | None] | None = None,
          stale_ok: bool = False) -> Decision:
    """Whether unattended work may start an agent under `acct` now. A reading
    older than READING_MAX_AGE is refreshed through `probe`; with `stale_ok`
    the newest reading stands whatever its age (a view that must not spend
    capacity to look)."""
    now = now or datetime.now(timezone.utc)
    if acct is None:
        return Decision(False, "this machine's AI account is unknown — is the Claude CLI "
                               "signed in?", now + RETRY_UNKNOWN)
    row = store.load_capacity(acct.key)
    pause = row.get("pause") or {}
    until = storekit.parse_ts(pause.get("until"))
    if until is not None and until > now:
        return Decision(False, f"paused: {pause.get('reason') or 'usage limit reached'}", until)
    p = policy(store, acct)
    if acct.billing == "api":
        if p.daily_budget_usd is None:
            return Decision(False, "no daily budget is set for this API key", None)
        start, tomorrow = local_midnights(p, now)
        spent = store.capacity_spend(acct.key, _iso(start))
        if spent >= p.daily_budget_usd:
            return Decision(False, f"today's budget of ${p.daily_budget_usd:.2f} is spent "
                                   f"(${spent:.2f})", tomorrow)
        return Decision(True, f"${spent:.2f} of ${p.daily_budget_usd:.2f} spent today", None)
    reading = reading_from_dict(row.get("reading") or {})
    if reading is not None and not stale_ok and now - reading.at > READING_MAX_AGE:
        reading = None
    failed = _probe_failed.get(acct.key)
    if reading is None and probe is not None and (failed is None or now - failed >= RETRY_UNKNOWN):
        reading = probe()
        if reading is None:
            _probe_failed[acct.key] = now
        else:
            _probe_failed.pop(acct.key, None)
    five = _current(reading.five_hour, now, FIVE_HOURS) if reading is not None else None
    if reading is None or five is None:
        return Decision(False, "no capacity reading from the Claude CLI", now + RETRY_UNKNOWN)
    cap, boundary = cap_now(p, now)
    used = five.utilization
    if used >= cap:
        later = cap_now(p, boundary + timedelta(seconds=1))[0]
        retry = min(five.resets_at, boundary) if used < later else five.resets_at
        return Decision(False, f"5-hour window at {used:.0%} (cap now {cap:.0%})", retry)
    week = _current(reading.seven_day, now, WEEK)
    if p.weekly_pacing and week is not None:
        line = pacing_line(week, now)
        if week.utilization > line:
            start = week.resets_at - WEEK
            return Decision(False, f"weekly use {week.utilization:.0%} is ahead of pace "
                                   f"({line:.0%} by now)",
                            start + WEEK * (week.utilization - WEEKLY_SLACK))
    return Decision(True, f"5-hour window at {used:.0%} (cap now {cap:.0%})", None)


# --- the unattended mark ----------------------------------------------------

# None defers to UNATTENDED_ENV; "" marks a block attended whatever the env says.
_LANE: ContextVar[str | None] = ContextVar("capacity_lane", default=None)


@contextmanager
def unattended(lane: str) -> Iterator[None]:
    """Mark the agent work run inside this block as `lane`'s unattended work."""
    token = _LANE.set(lane)
    try:
        yield
    finally:
        _LANE.reset(token)


@contextmanager
def attended() -> Iterator[None]:
    """Mark the agent work run inside this block as not gated — the probe that
    refreshes a reading runs this way."""
    token = _LANE.set("")
    try:
        yield
    finally:
        _LANE.reset(token)


def current_lane() -> str | None:
    """The unattended batch lane this agent call is gated under, or None."""
    lane = _LANE.get()
    if lane is not None:
        return lane or None
    return os.environ.get(UNATTENDED_ENV) or None


_METER: ContextVar[str | None] = ContextVar("capacity_meter", default=None)


@contextmanager
def metered(lane: str) -> Iterator[None]:
    """Book the agent runs inside this block as `lane`'s unattended spend,
    without gating each of them."""
    token = _METER.set(lane)
    try:
        yield
    finally:
        _METER.reset(token)


def meter_lane() -> str | None:
    """The lane an agent run's spend is booked under: the unattended batch lane,
    else the metering mark (this context, then METER_ENV); None for work an
    operator started."""
    lane = current_lane()
    if lane is not None:
        return lane
    return _METER.get() or os.environ.get(METER_ENV) or None
