"""What the app shows of each AI account's capacity (pipeline/capacity.py is
the policy): the accounts the workers report and this machine's own, each
with its policy, newest reading, the cap in effect, the pacing line, whether
unattended work may start, and the cost of today's background agent runs by
lane at API prices (a real charge only on an API-key account). A view never
spends capacity to look: decisions read the newest reading whatever its age.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TypedDict
from zoneinfo import ZoneInfo

from pipeline import capacity
from prospector_app.backend import data


class DecisionView(TypedDict):
    allowed: bool
    reason: str
    retry_at: str | None


class CapacityHealthItem(TypedDict):
    kind: str
    severity: str
    label: str
    detail: str | None
    host: str | None


class AccountView(TypedDict):
    key: str
    label: str
    billing: str
    machines: list[str]
    this_machine: bool
    policy: dict
    policy_saved: bool
    reading: dict | None
    reading_age_seconds: float | None
    cap_now: float | None
    next_boundary: str | None
    pacing_line: float | None
    weekly_resets_at: str | None
    decision: DecisionView
    cost_today_by_lane: dict[str, float]


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _known_accounts() -> tuple[dict[str, capacity.Account], dict[str, list[str]],
                               capacity.Account | None]:
    """Every account a worker has stamped on its health record, the machines
    on each, and this machine's own account."""
    accounts: dict[str, capacity.Account] = {}
    machines: dict[str, list[str]] = {}
    for host, rec in (data.store().load_worker_health().get("hosts") or {}).items():
        stamp = rec.get("ai_account") if isinstance(rec, dict) else None
        if not isinstance(stamp, dict) or not stamp.get("key"):
            continue
        key = str(stamp["key"])
        billing = "api" if stamp.get("billing") == "api" else "subscription"
        accounts.setdefault(key, capacity.Account(key=key, billing=billing, plan=None,
                                                  label=str(stamp.get("label") or key)))
        machines.setdefault(key, []).append(str(host))
    mine = capacity.account()
    if mine is not None:
        accounts[mine.key] = mine
        machines.setdefault(mine.key, [])
    return accounts, machines, mine


def view(acct: capacity.Account, machines: list[str], this_machine: bool,
         now: datetime | None = None) -> AccountView:
    now = now or datetime.now(timezone.utc)
    st = data.store()
    p = capacity.policy(st, acct)
    reading = capacity.reading_from_dict(st.load_capacity(acct.key).get("reading") or {})
    decision = capacity.check(st, acct, now=now, stale_ok=True)
    cap, boundary = capacity.cap_now(p, now)
    week = reading.seven_day if reading is not None else None
    cost: dict[str, float] = {}
    start, _ = capacity.local_midnights(p, now)
    for lane, usd in st.agent_cost(_iso(start) or "", "lane", account=acct.key,
                                    unattended=True).items():
        key = lane or "unknown"
        cost[key] = round(cost.get(key, 0.0) + usd, 4)
    subscription = acct.billing == "subscription"
    return {
        "key": acct.key, "label": acct.label, "billing": acct.billing,
        "machines": sorted(machines), "this_machine": this_machine,
        "policy": capacity.policy_to_dict(p), "policy_saved": p.saved,
        "reading": None if reading is None else capacity.reading_to_dict(reading),
        "reading_age_seconds": None if reading is None else (now - reading.at).total_seconds(),
        "cap_now": cap if subscription else None,
        "next_boundary": _iso(boundary) if subscription else None,
        "pacing_line": (capacity.pacing_line(week, now)
                        if subscription and week is not None and p.weekly_pacing else None),
        "weekly_resets_at": _iso(week.resets_at) if week is not None else None,
        "decision": {"allowed": decision.allowed, "reason": decision.reason,
                     "retry_at": _iso(decision.retry_at)},
        "cost_today_by_lane": cost,
    }


def accounts() -> list[AccountView]:
    """Every known account, this machine's first."""
    known, machines, mine = _known_accounts()
    mine_key = mine.key if mine is not None else None
    views = [view(acct, machines.get(key, []), key == mine_key) for key, acct in known.items()]
    return sorted(views, key=lambda v: (not v["this_machine"], v["label"]))


def this_account() -> capacity.Account | None:
    return capacity.account()


# The health strip polls from every open page; the capacity lines are held this long.
_HEALTH_TTL_SECONDS = 30.0
_health_cache: tuple[float, list[CapacityHealthItem]] | None = None


def health_items() -> list[CapacityHealthItem]:
    """One amber strip line per account whose unattended work is paused and
    that some worker machine runs under."""
    global _health_cache
    now_ts = time.monotonic()
    if _health_cache is not None and now_ts - _health_cache[0] < _HEALTH_TTL_SECONDS:
        return _health_cache[1]
    items: list[CapacityHealthItem] = []
    for v in accounts():
        if not v["machines"] or v["decision"]["allowed"]:
            continue
        retry = v["decision"]["retry_at"]
        resumes = ""
        if retry:
            local = datetime.fromisoformat(retry).astimezone(ZoneInfo(v["policy"]["timezone"]))
            resumes = f" · resumes ~{local:%I:%M %p}".replace("~0", "~")
        items.append({"kind": "capacity", "severity": "amber",
                      "label": f"Background AI paused · {v['label']}",
                      "detail": f"{v['decision']['reason']}{resumes}", "host": None})
    _health_cache = (now_ts, items)
    return items


def forget_health() -> None:
    global _health_cache
    _health_cache = None
