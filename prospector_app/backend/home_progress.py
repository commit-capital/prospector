"""The Home progress row: whether the factory is winning, in four tiles.

Each tile is one number, a daily series for its sparkline, and a
week-over-week delta, derived on read from the store snapshot, the activity
log, and the runs ledger — nothing here is stored. The caller supplies every
input, so the derivation is pure and the endpoint owns the reads.

The series measure Prospector's own record: "resolved" counts landed merge and
close actions from the activity log (an upstream close by someone else appears
nowhere), and "incoming" counts the PRs and issues the store has ingested. The
`last_ingest_at` stamp rides along so the tiles can grey the days after the
last successful ingest instead of drawing them as zero.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

from pipeline.storekit import PhaseRun, RunRecord
from prospector_app.backend import activity

if TYPE_CHECKING:
    from pipeline.model import Pr

# Landed activity kinds that resolve an item (a reopen un-resolves, so it is
# deliberately not here).
_RESOLVE_KINDS = frozenset({"close", "merge", "issue-close"})

# fix:single endings that are the agent's own call on the request. `failed` is
# the machine's fault and `cancelled` a re-arm, so neither is a decision.
_FIX_DECIDED = frozenset({"pushed", "approved", "awaiting-review", "refused"})
# The endings that hand the request to a person instead of acting on it.
_FIX_ESCALATED = frozenset({"awaiting-review", "refused"})

# VERIFY outcomes where the automation proved the fix on its own; every other
# concluded outcome hands the PR to a person (the operator or the author).
_VERIFY_CLEAN = frozenset({"verified-fix", "agent-verified"})


def _last_ingest_at(runs: list[RunRecord]) -> str | None:
    """When the last full ingest finished, from the whole runs ledger."""
    best: str | None = None
    for rec in runs:
        if isinstance(rec, PhaseRun) and rec.phase == "ingest":
            stamp = rec.finished or rec.started
            if stamp is not None and (best is None or stamp > best):
                best = stamp
    return best


def _run_day(rec: PhaseRun, tz: tzinfo) -> str:
    return activity.local_day(rec.finished or rec.started, tz)


def _is_auto_resolution(ev: dict) -> bool:
    """A resolution that landed as a side-effect of another action (its `via`
    names the carrier, e.g. an issue closed by a merge) rather than as its own
    operator click."""
    return bool(ev.get("via"))


def _decision(rec: PhaseRun) -> tuple[bool, bool]:
    """(is a decision, was escalated to a person) for one lane ledger entry."""
    stats = rec.raw.get("stats") or {}
    if rec.phase == "fix:single":
        status = stats.get("status")
        return status in _FIX_DECIDED, status in _FIX_ESCALATED
    if rec.phase == "verify:single":
        outcome = stats.get("outcome")
        return bool(outcome), bool(outcome) and outcome not in _VERIFY_CLEAN
    if rec.phase == "security:review-one":
        verdict = stats.get("verdict")
        return bool(verdict), verdict == "RED"
    return False, False


def _rate(escalated: int, decisions: int) -> float | None:
    return round(escalated / decisions, 3) if decisions else None


def _median(hours: list[float]) -> float | None:
    return round(statistics.median(hours), 1) if hours else None


def progress(prs: dict[int, Pr], issues: list[dict], events: list[dict],
             runs: list[RunRecord], *, today: date | None = None,
             tz: tzinfo | None = None, n_days: int = 30) -> dict:
    """The four tiles' numbers, series, and deltas over the last `n_days`
    local days. `today` and `tz` override the wall clock — pass them in tests
    for determinism. Deltas compare the last 7 days to the 7 before them, so
    `n_days` must cover at least 14."""
    local_tz = tz or datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc
    today_d = today or datetime.now(local_tz).date()
    days = [(today_d - timedelta(days=i)).isoformat() for i in range(n_days - 1, -1, -1)]
    day_idx = {d: i for i, d in enumerate(days)}
    last7 = days[-7:]
    prev7 = days[-14:-7]

    incoming = [0] * n_days
    for rec in prs.values():
        i = day_idx.get(activity.local_day(rec.created_at, local_tz))
        if i is not None:
            incoming[i] += 1
    for iss in issues:
        i = day_idx.get(activity.local_day(iss.get("created_at"), local_tz))
        if i is not None:
            incoming[i] += 1

    resolved = [0] * n_days
    auto = [0] * n_days
    first_event_at: dict[int, str] = {}
    for ev in events:
        if not activity.is_landed(ev):
            continue
        n = ev.get("pr")
        at = ev.get("at")
        if isinstance(n, int) and isinstance(at, str):
            prior = first_event_at.get(n)
            if prior is None or at < prior:
                first_event_at[n] = at
        if ev.get("kind") not in _RESOLVE_KINDS:
            continue
        i = day_idx.get(activity.local_day(at, local_tz))
        if i is not None:
            resolved[i] += 1
            if _is_auto_resolution(ev):
                auto[i] += 1

    open_now = (sum(1 for p in prs.values() if p.state == "open")
                + sum(1 for iss in issues if iss.get("state") == "open"))
    # The backlog at each day's close, walked backward from today's true count
    # by each day's net flow — the sparkline is exact at its right edge and an
    # estimate leftward (an item created and resolved outside the store's view
    # never moves it).
    backlog = [0] * n_days
    backlog[-1] = open_now
    for i in range(n_days - 2, -1, -1):
        backlog[i] = backlog[i + 1] - (incoming[i + 1] - resolved[i + 1])

    decisions = [0] * n_days
    escalated = [0] * n_days
    first_run_at: dict[int, str] = {}
    for rec in runs:
        if not isinstance(rec, PhaseRun):
            continue
        n = rec.raw.get("pr")
        stamp = rec.started or rec.finished
        if isinstance(n, int) and isinstance(stamp, str):
            prior = first_run_at.get(n)
            if prior is None or stamp < prior:
                first_run_at[n] = stamp
        decided, esc = _decision(rec)
        if decided:
            i = day_idx.get(_run_day(rec, local_tz))
            if i is not None:
                decisions[i] += 1
                if esc:
                    escalated[i] += 1

    # Hours from each PR's creation to Prospector's first touch (an activity
    # event or a per-PR ledger run), bucketed by the PR's creation day.
    touch_hours: list[list[float]] = [[] for _ in days]
    for n, rec in prs.items():
        i = day_idx.get(activity.local_day(rec.created_at, local_tz))
        if i is None:
            continue
        touches = [t for t in (first_event_at.get(n), first_run_at.get(n)) if t is not None]
        created = rec.created_at
        if not touches or created is None:
            continue
        try:
            dt = (datetime.fromisoformat(min(touches).replace("Z", "+00:00"))
                  - datetime.fromisoformat(created.replace("Z", "+00:00")))
        except ValueError:
            continue
        hours = dt.total_seconds() / 3600
        if hours >= 0:
            touch_hours[i].append(hours)

    def week(series: list[int], which: list[str]) -> int:
        return sum(series[day_idx[d]] for d in which)

    week_hours = [h for d in last7 for h in touch_hours[day_idx[d]]]
    prev_hours = [h for d in prev7 for h in touch_hours[day_idx[d]]]
    return {
        "days": days,
        "last_ingest_at": _last_ingest_at(runs),
        "backlog": {
            "current": open_now,
            "series": backlog,
            "delta_7d": backlog[-1] - backlog[-8],
        },
        "resolved": {
            "series": resolved,
            "week_total": week(resolved, last7),
            "prev_week_total": week(resolved, prev7),
            "auto_7d": week(auto, last7),
            "person_7d": week(resolved, last7) - week(auto, last7),
        },
        "escalation": {
            "series": [_rate(escalated[i], decisions[i]) for i in range(n_days)],
            "rate_7d": _rate(week(escalated, last7), week(decisions, last7)),
            "prev_rate_7d": _rate(week(escalated, prev7), week(decisions, prev7)),
            "decisions_7d": week(decisions, last7),
            "escalated_7d": week(escalated, last7),
        },
        "first_action": {
            "series": [_median(h) for h in touch_hours],
            "median_hours_7d": _median(week_hours),
            "prev_median_hours_7d": _median(prev_hours),
            "sampled_7d": len(week_hours),
        },
    }
