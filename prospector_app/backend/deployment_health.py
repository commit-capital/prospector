"""Cross-machine deployment health for the strip every page shows.

One compact answer over the shared store: which worker lanes are paused and on
which machine, which workers went dark, and how stale ingest is. Each problem
carries the page that fixes it, and the summary says whether any live,
untripped worker is left to drain the queues — the Home "In motion" column
reads that as Stalled.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Literal, TypedDict

from pipeline import storekit, worker_health
from prospector_app.backend import data, escalation, fix_queue, verify_queue

# Ingest ages past these read amber, then red, in the strip.
INGEST_AMBER_SECONDS = 24 * 3600
INGEST_RED_SECONDS = 3 * 24 * 3600

# How long one computed summary is served before it is recomputed — every open
# page polls, so the ledger and registry reads run per backend, not per client.
CACHE_SECONDS = 10.0

# Which heartbeat registry's hosts run each lane: one verify worker runs both
# the security and verify lanes, a fix worker runs the fix lane.
LANE_REGISTRY: dict[str, str] = {"security": "verify", "verify": "verify", "fix": "fix"}

ItemLevel = Literal["amber", "red"]
Level = Literal["ok", "amber", "red"]


class HealthItem(TypedDict):
    key: str
    level: ItemLevel
    text: str
    detail: str | None
    to: str


class HealthSummary(TypedDict):
    level: Level
    items: list[HealthItem]
    auto_stalled: bool
    stalled_reason: str | None
    lanes_down: list[str]


_cache_lock = threading.Lock()
_cache: tuple[float, HealthSummary] | None = None


def summary() -> HealthSummary:
    """The strip's feed, recomputed at most every CACHE_SECONDS."""
    global _cache
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        result = compute()
        _cache = (time.monotonic(), result)
        return result


def _age_seconds(stamp: object) -> float | None:
    """Seconds since an ISO stamp; None when it is missing or unreadable."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds()


def _age_label(seconds: float) -> str:
    """A short age for strip text: minutes under an hour, hours under 36, days
    beyond."""
    if seconds >= 36 * 3600:
        return f"{seconds / 86400:.0f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{max(seconds, 0) / 60:.0f}m"


def _last_phase_run(records: list[storekit.RunRecord], phase: str) -> str | None:
    """The finished stamp of the ledger's most recent `phase` run."""
    latest: str | None = None
    for rec in records:
        if not isinstance(rec, storekit.PhaseRun) or rec.phase != phase:
            continue
        finished = rec.finished or rec.started
        if finished and (latest is None or finished > latest):
            latest = finished
    return latest


def _pr_ingest_at() -> str | None:
    return _last_phase_run(data.runs(), "ingest")


def _issue_ingest_at() -> str | None:
    """The issue pipeline's last ingest, from its own runs ledger; None when
    that store cannot be read."""
    try:
        from prospector_app.backend import issues
        return _last_phase_run(issues.cached_runs(), "ingest")
    except Exception:
        traceback.print_exc()
        return None


def _tripped_items(health_hosts: dict[str, dict]) -> list[HealthItem]:
    """One red item per worker with tripped lanes, naming the lanes, the
    machine, and the trip kinds; the first trip's reason rides as detail."""
    items: list[HealthItem] = []
    for host in sorted(health_hosts):
        rec = health_hosts[host] or {}
        tripped = [(name, (rec.get("lanes") or {}).get(name, {}).get("tripped") or {})
                   for name in worker_health.LANES
                   if worker_health.is_tripped(rec, name)]
        if not tripped:
            continue
        names = ", ".join(name for name, _ in tripped)
        kinds = sorted({str(t.get("kind") or "unknown") for _, t in tripped})
        reason = str(tripped[0][1].get("reason") or "") or None
        items.append({
            "key": f"tripped:{host}",
            "level": "red",
            "text": f"{names} lane{'s' if len(tripped) > 1 else ''} paused on {host} "
                    f"({', '.join(kinds)})",
            "detail": reason,
            "to": "/control",
        })
    return items


def _offline_items() -> list[HealthItem]:
    """One red item per worker gone dark — a stale heartbeat, not a clean stop
    (a worker stopping on purpose drops its registry entry)."""
    oldest: dict[str, float] = {}
    for w in escalation.offline_workers():
        host = str(w["host"])
        oldest[host] = max(oldest.get(host, 0.0), float(w["age_seconds"]))
    return [{
        "key": f"offline:{host}",
        "level": "red",
        "text": f"worker {host} offline · last beat {_age_label(age)} ago",
        "detail": None,
        "to": "/control",
    } for host, age in sorted(oldest.items())]


def _ingest_items() -> list[HealthItem]:
    """An item per ingest lane whose last run is past the amber threshold; a
    ledger that holds no ingest run yet says nothing."""
    items: list[HealthItem] = []
    for key, label, stamp in (("pr", "PR ingest", _pr_ingest_at()),
                              ("issues", "issue ingest", _issue_ingest_at())):
        age = _age_seconds(stamp)
        if age is None or age < INGEST_AMBER_SECONDS:
            continue
        level: ItemLevel = "red" if age >= INGEST_RED_SECONDS else "amber"
        items.append({
            "key": f"ingest:{key}",
            "level": level,
            "text": f"{label} {_age_label(age)} old",
            "detail": f"last run {stamp}",
            "to": "/control",
        })
    return items


def _lanes_down(health_hosts: dict[str, dict],
                registries: dict[str, list[dict]]) -> list[str]:
    """The lanes with at least one known worker and no live, untripped one. A
    lane no worker has ever run is unknown, not down."""
    online = {reg: {str(r.get("host")): (fix_queue if reg == "fix" else verify_queue)
              .beat_online(r.get("last_beat"))
              for r in records if r.get("host")}
              for reg, records in registries.items()}
    down: list[str] = []
    for lane in worker_health.LANES:
        hosts = online[LANE_REGISTRY[lane]]
        if hosts and all(not alive or worker_health.is_tripped(health_hosts.get(h) or {}, lane)
                         for h, alive in hosts.items()):
            down.append(lane)
    return down


def compute() -> HealthSummary:
    """The summary itself, uncached: every worker's registry heartbeat and
    lane health plus the two ingest ledgers, all read from the shared store,
    so any machine's outage reaches any app."""
    st = data.store()
    health_hosts: dict[str, dict] = st.load_worker_health().get("hosts") or {}
    registries = {"verify": verify_queue.worker_records(st.load_verify_worker()),
                  "fix": fix_queue.worker_records(st.load_fix_worker())}

    items = _tripped_items(health_hosts) + _offline_items() + _ingest_items()
    lanes_down = _lanes_down(health_hosts, registries)
    known = [lane for lane in worker_health.LANES
             if any(r.get("host") for r in registries[LANE_REGISTRY[lane]])]
    auto_stalled = bool(known) and all(lane in lanes_down for lane in known)
    stalled_reason = (f"no live, untripped worker on any lane "
                      f"({', '.join(lanes_down)}) — nothing is draining the queues"
                      if auto_stalled else None)
    level: Level = ("red" if any(i["level"] == "red" for i in items)
                    else "amber" if items else "ok")
    return {"level": level, "items": items, "auto_stalled": auto_stalled,
            "stalled_reason": stalled_reason, "lanes_down": lanes_down}
