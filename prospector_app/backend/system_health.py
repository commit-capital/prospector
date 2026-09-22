"""Deployment-wide health for the strip every page shows.

Folds the worker-health registry (tripped lanes), the two worker heartbeat
registries (silent machines), and the three ingest ledgers (stale data) into
a short list of strip items — each naming the cause and the machine, with the
app route that holds the fix — plus one `stalled` verdict for the Home
"In motion" column: whether any lane anywhere can still pick work. Every page
polls this, so the computed answer is cached briefly.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal, TypedDict

from pipeline import storekit, worker_health
from prospector_app.backend import data, escalation, fix_queue, verify_queue


class StripItem(TypedDict):
    severity: Literal["amber", "red"]
    text: str
    href: str


class LaneStatus(TypedDict):
    hosts: int
    ok: bool


# Ingest age at which a feed appears on the strip, and at which it turns red.
INGEST_AMBER_SECONDS = 24 * 3600
INGEST_RED_SECONDS = 3 * 24 * 3600
# How long one computed answer serves the polls before the sources are re-read.
CACHE_SECONDS = 10.0
# How much of a trip's reason the strip carries.
REASON_CHARS = 140

_cache: tuple[float, dict] | None = None


def _age_seconds(stamp: object, now: datetime) -> float | None:
    """Seconds from `stamp` to `now`, or None for a missing/unreadable stamp."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds()


def _age_text(seconds: float) -> str:
    """A compact age: '19d', '6h', '45m'."""
    if seconds >= 48 * 3600:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    return f"{max(0, int(seconds // 60))}m"


def _lane_hosts() -> dict[str, dict[str, dict]]:
    """Each lane's heartbeat records by host. Security reviews run on verify
    workers, so the security lane reads the verify registry."""
    st = data.store()
    verify_hosts = dict(st.load_verify_worker().get("hosts") or {})
    fix_hosts = dict(st.load_fix_worker().get("hosts") or {})
    return {"security": verify_hosts, "verify": verify_hosts, "fix": fix_hosts}


def _lane_status(health_hosts: dict[str, dict],
                 lane_hosts: dict[str, dict[str, dict]],
                 now: datetime) -> dict[str, LaneStatus]:
    """Per lane: how many machines' workers have ever registered for it, and
    whether at least one of them is beating with the lane open."""
    beat_limit = {"security": verify_queue.STALE_BEAT_SECONDS,
                  "verify": verify_queue.STALE_BEAT_SECONDS,
                  "fix": fix_queue.STALE_BEAT_SECONDS}
    out: dict[str, LaneStatus] = {}
    for lane_name in worker_health.LANES:
        hosts = lane_hosts[lane_name]
        ok = False
        for host, rec in hosts.items():
            age = _age_seconds(rec.get("last_beat"), now)
            if age is None or age >= beat_limit[lane_name]:
                continue
            if not worker_health.is_tripped(health_hosts.get(host) or {}, lane_name):
                ok = True
                break
        out[lane_name] = {"hosts": len(hosts), "ok": ok}
    return out


def _tripped_items(health_hosts: dict[str, dict],
                   lane_hosts: dict[str, dict[str, dict]],
                   offline: set[str]) -> list[StripItem]:
    """One red item per machine with tripped lanes, skipping machines already
    reported offline: their trip detail is older than their silence."""
    items: list[StripItem] = []
    for host, rec in sorted(health_hosts.items()):
        if host in offline:
            continue
        tripped = sorted(n for n in worker_health.LANES
                         if worker_health.is_tripped(rec, n))
        if not tripped:
            continue
        runs = {n for n, hosts in lane_hosts.items() if host in hosts}
        count = (f"{len(tripped)}/{len(runs)}" if len(runs) >= len(tripped)
                 else str(len(tripped)))
        first = worker_health.lane(rec, tripped[0]).get("tripped") or {}
        kind = str(first.get("kind") or "unknown")
        reason = str(first.get("reason") or "")[:REASON_CHARS]
        detail = f"{kind}: {reason}" if reason else kind
        items.append({"severity": "red",
                      "text": f"{count} lane{'s' if len(tripped) > 1 else ''} "
                              f"paused on {host} — {detail}",
                      "href": "/control"})
    return items


def _offline_items(now: datetime) -> tuple[list[StripItem], set[str]]:
    """One red item per machine whose heartbeat has been silent past the
    offline threshold, plus the set of those hosts."""
    items: list[StripItem] = []
    hosts: set[str] = set()
    for w in escalation.offline_workers(now):
        host = str(w["host"])
        if host in hosts:
            continue
        hosts.add(host)
        items.append({"severity": "red",
                      "text": f"worker {host} offline — no heartbeat for "
                              f"{_age_text(float(w['age_seconds']))}",
                      "href": "/control"})
    return items, hosts


def _last_phase_run(records: list[storekit.RunRecord], phase: str) -> str | None:
    """The most recent `phase` run's finish stamp, read newest-first from the
    append-ordered ledger."""
    for rec in reversed(records):
        if isinstance(rec, storekit.PhaseRun) and rec.phase == phase:
            return rec.finished or rec.started
    return None


def _issue_runs() -> list[storekit.RunRecord]:
    from prospector_app.backend import issues
    return issues.cached_runs()


def _alert_runs() -> list[storekit.RunRecord]:
    from prospector_app.backend import alert_data
    return alert_data.runs()


def _ingest_items(now: datetime) -> list[StripItem]:
    """One item per ingest feed older than the amber threshold. A feed with no
    run on record stays silent — a deployment may not use it at all."""
    feeds: tuple[tuple[str, str | None], ...] = (
        ("PR ingest", _last_phase_run(data.runs(), "ingest")),
        ("issue ingest", _last_phase_run(_issue_runs(), "ingest")),
        ("alert ingest", _last_phase_run(_alert_runs(), "alert-ingest")),
    )
    items: list[StripItem] = []
    for label, last in feeds:
        age = _age_seconds(last, now)
        if age is None or age < INGEST_AMBER_SECONDS:
            continue
        severity: Literal["amber", "red"] = ("red" if age >= INGEST_RED_SECONDS
                                             else "amber")
        items.append({"severity": severity,
                      "text": f"{label} {_age_text(age)} old",
                      "href": "/control"})
    return items


def compute(now: datetime | None = None) -> dict:
    """The strip's payload: `items` (red first), the worst `severity`
    (`ok`/`amber`/`red`), per-lane liveness, and `stalled` — no lane with any
    registered worker can pick work."""
    now = now or datetime.now(timezone.utc)
    health_hosts: dict[str, dict] = dict(
        data.store().load_worker_health().get("hosts") or {})
    lane_hosts = _lane_hosts()
    offline_items, offline_hosts = _offline_items(now)
    items = (offline_items
             + _tripped_items(health_hosts, lane_hosts, offline_hosts)
             + _ingest_items(now))
    items.sort(key=lambda i: i["severity"] != "red")
    lanes = _lane_status(health_hosts, lane_hosts, now)
    represented = [s for s in lanes.values() if s["hosts"] > 0]
    severity = ("red" if any(i["severity"] == "red" for i in items)
                else "amber" if items else "ok")
    return {"items": items, "severity": severity, "lanes": lanes,
            "stalled": bool(represented) and not any(s["ok"] for s in represented)}


def status() -> dict:
    """`compute`, served from a short cache: every open page polls this."""
    global _cache
    t = time.monotonic()
    if _cache and t - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    out = compute()
    _cache = (t, out)
    return out
