"""The global health strip: one read that says whether the automation is
running, across every machine on the store.

`build` is pure over its gathered inputs; `strip` gathers them from the shared
store — the worker-health registry (tripped lanes), both lanes' heartbeat
registries (the lane population and each worker's silence), and the runs
ledger (ingest recency). It rides `/api/status/now`, the feed every page's
header already polls, so a paused lane or a stale ingest reaches every page
within one poll. Each item names the cause and the machine and links to the
view that holds the fix.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TypedDict

from pipeline import storekit
from prospector_app.backend import data, escalation


class StripItem(TypedDict):
    severity: str  # "amber" | "red"
    text: str      # the cause and the machine it is on
    link: str      # the app route that opens the fix


class HealthStrip(TypedDict):
    level: str     # "ok" | "amber" | "red"
    items: list[StripItem]
    lanes_total: int
    lanes_down: int
    ingest_age_hours: float | None
    stalled: bool  # every known lane is paused or its worker is silent


# An ingest older than the first reads amber on the strip, older than the
# second reads red.
INGEST_AMBER_HOURS = 24.0
INGEST_RED_HOURS = 72.0

# How long one ledger scan's ingest timestamp serves polls before rescanning.
_INGEST_CACHE_SECONDS = 60.0

_ingest_cache: tuple[float, str | None] | None = None


def _age_text(hours: float) -> str:
    return f"{int(hours // 24)}d" if hours >= 48 else f"{int(hours)}h"


def _hours_since(iso: str, now: datetime) -> float | None:
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (now - then).total_seconds() / 3600)


def build(health: dict, offline: list[dict], lane_pairs: set[tuple[str, str]],
          ingest_last: str | None, now: datetime) -> HealthStrip:
    """The strip from its inputs: `health` is `escalation.health_status()`,
    `offline` is `escalation.offline_workers()`, `lane_pairs` the (host, lane)
    population from the heartbeat registries, `ingest_last` the latest ingest
    run's finish. A lane is down when it is tripped or its worker is silent;
    `stalled` says no lane anywhere can pick work."""
    pairs = set(lane_pairs)
    tripped_by_host: dict[str, list[str]] = {}
    kind_by_host: dict[str, str] = {}
    for host_rec in health.get("hosts") or []:
        host = str(host_rec.get("host"))
        lanes = host_rec.get("lanes") or {}
        for name in host_rec.get("tripped") or []:
            pairs.add((host, name))
            tripped_by_host.setdefault(host, []).append(name)
            kind = ((lanes.get(name) or {}).get("tripped") or {}).get("kind")
            kind_by_host.setdefault(host, str(kind or "unknown"))
    offline_age: dict[str, float] = {}
    for w in offline:
        host = str(w["host"])
        offline_age[host] = max(offline_age.get(host, 0.0),
                                float(w.get("age_seconds") or 0.0))
    tripped_pairs = {(h, name) for h, names in tripped_by_host.items() for name in names}
    down = tripped_pairs | {(h, name) for (h, name) in pairs if h in offline_age}
    lanes_total, lanes_down = len(pairs), len(down)

    items: list[StripItem] = []
    if lanes_down:
        items.append({"severity": "red" if lanes_down == lanes_total else "amber",
                      "text": f"{lanes_down}/{lanes_total} lane{'s' if lanes_total != 1 else ''} paused",
                      "link": "/control"})
    for host in sorted(tripped_by_host):
        names = ", ".join(sorted(tripped_by_host[host]))
        items.append({"severity": "amber",
                      "text": f"{names} paused on {host} ({kind_by_host[host]})",
                      "link": "/control"})
    for host in sorted(offline_age):
        items.append({"severity": "red",
                      "text": f"no heartbeat from {host} for {_age_text(offline_age[host] / 3600)}",
                      "link": "/control"})

    age = _hours_since(ingest_last, now) if ingest_last else None
    if ingest_last is None:
        items.append({"severity": "amber", "text": "PR ingest has never run",
                      "link": "/control"})
    elif age is not None and age >= INGEST_AMBER_HOURS:
        items.append({"severity": "red" if age >= INGEST_RED_HOURS else "amber",
                      "text": f"PR ingest {_age_text(age)} old", "link": "/control"})

    level = ("red" if any(i["severity"] == "red" for i in items)
             else "amber" if items else "ok")
    return {"level": level, "items": items,
            "lanes_total": lanes_total, "lanes_down": lanes_down,
            "ingest_age_hours": round(age, 1) if age is not None else None,
            "stalled": lanes_total > 0 and lanes_down == lanes_total}


def lane_population(verify_reg: dict, fix_reg: dict) -> set[tuple[str, str]]:
    """Every (host, lane) the heartbeat registries know: a verify worker runs
    the security and verify lanes, a fix worker the fix lane. A cleanly stopped
    worker drops its record, so retired machines are not counted."""
    pairs: set[tuple[str, str]] = set()
    for host in verify_reg.get("hosts") or {}:
        pairs.add((str(host), "security"))
        pairs.add((str(host), "verify"))
    for host in fix_reg.get("hosts") or {}:
        pairs.add((str(host), "fix"))
    return pairs


def _last_ingest(now_ts: float) -> str | None:
    """The latest ingest run's finish from the runs ledger, cached briefly so
    the poll does not rescan the ledger on every request."""
    global _ingest_cache
    if _ingest_cache is not None and now_ts - _ingest_cache[0] < _INGEST_CACHE_SECONDS:
        return _ingest_cache[1]
    latest: str | None = None
    for rec in data.runs():
        if not isinstance(rec, storekit.PhaseRun) or rec.phase != "ingest":
            continue
        finished = rec.finished or rec.started
        if finished and (latest is None or finished > latest):
            latest = finished
    _ingest_cache = (now_ts, latest)
    return latest


def strip() -> HealthStrip:
    st = data.store()
    now = datetime.now(timezone.utc)
    return build(escalation.health_status(), escalation.offline_workers(now),
                 lane_population(st.load_verify_worker(), st.load_fix_worker()),
                 _last_ingest(time.time()), now)
