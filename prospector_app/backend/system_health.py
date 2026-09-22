"""One systemwide health verdict, for the strip every page shows.

Composes what the shared store already knows — every worker's tripped lanes,
every worker's heartbeat, and each ingest's last run — into a short list of
problem items with one overall severity, so an outage on any machine reaches
the operator on whatever page they have open. `summarize` is pure over plain
data; `status` gathers the live inputs.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import TypedDict

from pipeline import storekit
from prospector_app.backend import data, escalation, fix_queue, verify_queue


class HealthItem(TypedDict):
    kind: str
    severity: str
    label: str
    detail: str | None
    host: str | None


class SystemHealth(TypedDict):
    severity: str
    items: list[HealthItem]
    lanes_total: int
    lanes_down: int
    workers_stalled: bool


# How stale an ingest is before the strip colors it.
INGEST_AMBER_HOURS = 24.0
INGEST_RED_HOURS = 72.0

# The ingests the strip watches: key in `ingest_last` → human label.
INGEST_LABELS: dict[str, str] = {
    "pr": "PR ingest",
    "issues": "issue ingest",
    "alerts": "alert ingest",
}

_SEVERITY_RANK = {"ok": 0, "amber": 1, "red": 2}

# How long the PR store's latest-ingest stamp is held: the strip polls from
# every open page, and the runs ledger is a whole-table read.
_PR_INGEST_TTL_SECONDS = 60.0
_pr_ingest_cache: tuple[float, str | None] | None = None


def _hours_since(iso: str | None, now_ts: float) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None
    return max(0.0, (now_ts - then) / 3600)


def _age_label(hours: float) -> str:
    if hours < 1:
        return "under 1h"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def _ingest_item(label: str, last: str | None, now_ts: float) -> HealthItem | None:
    """The strip's item for one ingest, or None while it is fresh. An ingest
    that has never run reads as nothing to report: the strip flags a
    deployment that stopped ingesting, and the Control tab's suggestions own
    first-run nudges."""
    hours = _hours_since(last, now_ts)
    if hours is None or hours < INGEST_AMBER_HOURS:
        return None
    severity = "red" if hours >= INGEST_RED_HOURS else "amber"
    return {"kind": "ingest", "severity": severity, "host": None,
            "label": f"{label} {_age_label(hours)} old",
            "detail": f"last ran {last}"}


def summarize(health_hosts: list[dict], offline: list[dict],
              worker_lanes: list[tuple[str, str]],
              ingest_last: dict[str, str | None],
              now_ts: float | None = None) -> SystemHealth:
    """The strip's answer, from plain data: `health_hosts` is
    `escalation.health_status()["hosts"]`, `offline` is
    `escalation.offline_workers()`, `worker_lanes` every `(lane, host)` the
    worker registries name, and `ingest_last` each ingest's last-run stamp
    keyed per INGEST_LABELS."""
    now_ts = now_ts if now_ts is not None else time.time()
    tripped_by_host: dict[str, list[str]] = {
        str(h["host"]): list(h["tripped"]) for h in health_hosts if h.get("tripped")}
    offline_by_host: dict[str, float] = {}
    for o in offline:
        host = str(o["host"])
        offline_by_host[host] = max(float(o["age_seconds"]),
                                    offline_by_host.get(host, 0.0))

    # A tripped lane on a host the registries no longer name still counts.
    lanes = set(worker_lanes)
    for host, names in tripped_by_host.items():
        lanes.update((name, host) for name in names)
    down = {(lane, host) for lane, host in lanes
            if host in offline_by_host or lane in tripped_by_host.get(host, [])}

    items: list[HealthItem] = []
    if down:
        severity = "red" if len(down) == len(lanes) else "amber"
        items.append({"kind": "lanes", "severity": severity, "host": None,
                      "label": f"{len(down)}/{len(lanes)} worker lanes down",
                      "detail": None})
        for host in sorted(set(tripped_by_host) | set(offline_by_host)):
            if host in offline_by_host:
                items.append({
                    "kind": "offline", "severity": severity, "host": host,
                    "label": f"{host}: worker offline · silent "
                             f"{_age_label(offline_by_host[host] / 3600)}",
                    "detail": None})
                continue
            rec = next(h for h in health_hosts if str(h["host"]) == host)
            names = tripped_by_host[host]
            first = ((rec.get("lanes") or {}).get(names[0]) or {}).get("tripped") or {}
            items.append({
                "kind": "trip", "severity": severity, "host": host,
                "label": f"{host}: {', '.join(names)} paused · "
                         f"{first.get('kind') or 'unknown'}",
                "detail": str(first.get("reason") or "") or None})

    for key, label in INGEST_LABELS.items():
        item = _ingest_item(label, ingest_last.get(key), now_ts)
        if item:
            items.append(item)

    severity = max((i["severity"] for i in items),
                   key=lambda s: _SEVERITY_RANK[s], default="ok")
    return {"severity": severity, "items": items,
            "lanes_total": len(lanes), "lanes_down": len(down),
            "workers_stalled": bool(lanes) and down == lanes}


def _latest_run(records: list[storekit.RunRecord], phase: str) -> str | None:
    for rec in reversed(records):
        if isinstance(rec, storekit.PhaseRun) and rec.phase == phase:
            return rec.finished or rec.started
    return None


def _pr_ingest_last() -> str | None:
    global _pr_ingest_cache
    now = time.monotonic()
    if _pr_ingest_cache and now - _pr_ingest_cache[0] < _PR_INGEST_TTL_SECONDS:
        return _pr_ingest_cache[1]
    last = _latest_run(data.runs(), "ingest")
    _pr_ingest_cache = (now, last)
    return last


def status() -> SystemHealth:
    """Gather the live inputs and summarize, across every machine on this
    store."""
    from prospector_app.backend import alert_data, issues
    st = data.store()
    worker_lanes: list[tuple[str, str]] = []
    for rec in verify_queue.worker_records(st.load_verify_worker()):
        host = rec.get("host")
        if host:
            worker_lanes += [("security", str(host)), ("verify", str(host))]
    for rec in fix_queue.worker_records(st.load_fix_worker()):
        host = rec.get("host")
        if host:
            worker_lanes.append(("fix", str(host)))
    ingest_last: dict[str, str | None] = {
        "pr": _pr_ingest_last(),
        "issues": _latest_run(issues.cached_runs(), "ingest"),
        "alerts": _latest_run(alert_data.runs(), "alert-ingest"),
    }
    health = escalation.health_status()
    return summarize(health["hosts"], escalation.offline_workers(),
                     worker_lanes, ingest_last)
