"""The one aggregate behind the global health strip: every condition that
means the automation is not running normally, graded amber or red.

Collected across every machine sharing the store — tripped worker lanes and
silent heartbeats from the shared registries, ingest staleness from the three
runs ledgers — so any page's strip shows an outage wherever it lives. Each
item carries the in-app link to where it is fixed. `stalled` says no lane on
any machine can pick work right now; Home greys its worker column on it.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from pipeline import storekit
from prospector_app.backend import data, escalation, fix_queue, verify_queue

# Ingest ages at which the strip turns amber, then red. PRs are the pipeline's
# pulse and go stale fastest; issues and alerts are swept on longer cadences.
PR_INGEST_AMBER_SECONDS = 24 * 3600
PR_INGEST_RED_SECONDS = 3 * 24 * 3600
ISSUE_INGEST_AMBER_SECONDS = 3 * 24 * 3600
ISSUE_INGEST_RED_SECONDS = 7 * 24 * 3600
ALERT_INGEST_AMBER_SECONDS = 7 * 24 * 3600
ALERT_INGEST_RED_SECONDS = 14 * 24 * 3600

# How far back the PR runs ledger is read for an ingest run; a ledger with
# none in this window reads as "not run in the window", which is red on its
# own whenever there are PRs to keep fresh.
RUNS_LOOKBACK_DAYS = 60

# How long one computed answer serves the poll from every open tab.
CACHE_SECONDS = 10.0

_cache_lock = threading.Lock()
_cached: tuple[float, dict] | None = None


class StripItem(TypedDict):
    kind: str  # "lane-tripped" | "worker-offline" | "ingest-stale"
    severity: str  # "amber" | "red"
    text: str
    detail: str | None
    link: str
    host: str | None


def _age_seconds(stamp: object, now: datetime) -> float | None:
    """Seconds from `stamp` to `now`; an unreadable stamp reads as None."""
    try:
        at = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds()


def _age_label(seconds: float) -> str:
    """A compact age: '5m', '3h', '19d'."""
    seconds = max(seconds, 0.0)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 24 * 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // (24 * 3600))}d"


def _latest_phase(runs: list[storekit.RunRecord], phase: str) -> str | None:
    """The most recent finished-at stamp among `runs` for `phase`."""
    latest: str | None = None
    for rec in runs:
        if not isinstance(rec, storekit.PhaseRun) or rec.phase != phase:
            continue
        finished = rec.finished or rec.started
        if finished and (latest is None or finished > latest):
            latest = finished
    return latest


def _pr_ingest_at(now: datetime) -> str | None:
    since = (now - timedelta(days=RUNS_LOOKBACK_DAYS)).isoformat()
    return _latest_phase(data.runs(since=since), "ingest")


def _issue_ingest_at() -> str | None:
    """Last issue ingest, from the issue store's own ledger. None when the
    store is unreadable — the strip must never take a page down with it."""
    try:
        from prospector_app.backend import issues
        return _latest_phase(issues.cached_runs(), "ingest")
    except Exception:
        traceback.print_exc()
        return None


def _alert_ingest_at() -> str | None:
    """Last alert ingest, from the alert store's own ledger. None when the
    store is unreadable — the strip must never take a page down with it."""
    try:
        from prospector_app.backend import alert_data
        return _latest_phase(alert_data.runs(), "alert-ingest")
    except Exception:
        traceback.print_exc()
        return None


def _ingest_item(label: str, at: str | None, amber: int, red: int,
                 now: datetime) -> StripItem | None:
    """One ingest source's strip item, or None while it is fresh enough. A
    source with no run on record stays silent here — an unused source (alerts
    without the App permissions, a deployment not triaging issues) is not an
    outage; the PR case handles its own never-ran reading in `status`."""
    if at is None:
        return None
    age = _age_seconds(at, now)
    if age is None or age < amber:
        return None
    return {"kind": "ingest-stale",
            "severity": "red" if age >= red else "amber",
            "text": f"{label} {_age_label(age)} old",
            "detail": f"Last successful {label} finished {at}. "
                      "Run it from the Control tab.",
            "link": "/control", "host": None}


def _tripped_items(health_hosts: dict) -> list[StripItem]:
    """One red item per worker with tripped lanes: how many of its lanes are
    paused, on which machine, and the trip kinds; the first reason in full as
    the hover detail."""
    items: list[StripItem] = []
    for host, rec in sorted(health_hosts.items()):
        lanes = rec.get("lanes") or {}
        tripped = {name: entry.get("tripped") or {}
                   for name, entry in sorted(lanes.items()) if entry.get("tripped")}
        if not tripped:
            continue
        kinds = sorted({str(t.get("kind") or "unknown") for t in tripped.values()})
        first = next(iter(tripped.values()))
        noun = "lane" if len(tripped) == 1 else "lanes"
        items.append({
            "kind": "lane-tripped", "severity": "red",
            "text": f"{len(tripped)}/{len(lanes)} {noun} paused on {host}"
                    f" · {', '.join(kinds)}",
            "detail": str(first.get("reason") or "") or None,
            "link": "/control", "host": str(host)})
    return items


def _offline_items(now: datetime) -> list[StripItem]:
    """One red item per worker whose heartbeat has been silent past the
    escalation threshold. A worker that stopped on purpose drops its record,
    so silence here is a crash, not a shutdown."""
    items: list[StripItem] = []
    seen: set[str] = set()
    for w in escalation.offline_workers(now):
        host = str(w["host"])
        if host in seen:
            continue
        seen.add(host)
        items.append({
            "kind": "worker-offline", "severity": "red",
            "text": f"worker {host} offline · last beat "
                    f"{_age_label(float(w['age_seconds']))} ago",
            "detail": f"No heartbeat from {host} since {w['last_beat']}. Its claimed "
                      "runs are reclaimed by other workers once the beat is stale; "
                      "bring the machine back or clear its registry entries.",
            "link": "/control", "host": host})
    return items


def _stalled(health_hosts: dict, verify_reg: dict, fix_reg: dict) -> bool:
    """Whether no lane on any known worker can pick work: every registered
    worker is either offline (stale heartbeat) or tripped on each lane it
    runs. False while no worker has ever registered — a deployment without
    workers has nothing stalled, just nothing running."""
    entries: list[bool] = []
    for lane_names, reg, online in (
            (("security", "verify"), verify_reg, verify_queue.beat_online),
            (("fix",), fix_reg, fix_queue.beat_online)):
        for host, r in (reg.get("hosts") or {}).items():
            rec = health_hosts.get(host) or {}
            for name in lane_names:
                tripped = bool(((rec.get("lanes") or {}).get(name) or {}).get("tripped"))
                entries.append(online(r.get("last_beat")) and not tripped)
    return bool(entries) and not any(entries)


def status(now: datetime | None = None) -> dict:
    """The strip's whole answer: `items` (red first) and `stalled`."""
    now = now or datetime.now(timezone.utc)
    st = data.store()
    health_hosts = st.load_worker_health().get("hosts") or {}

    items = _tripped_items(health_hosts) + _offline_items(now)

    pr_at = _pr_ingest_at(now)
    if pr_at is None and data.prs():
        items.append({
            "kind": "ingest-stale", "severity": "red",
            "text": f"PR ingest not run in {RUNS_LOOKBACK_DAYS}d",
            "detail": "No ingest run in the ledger window while the store holds "
                      "PRs. Run ingest from the Control tab.",
            "link": "/control", "host": None})
    for label, at, amber, red in (
            ("PR ingest", pr_at, PR_INGEST_AMBER_SECONDS, PR_INGEST_RED_SECONDS),
            ("issue ingest", _issue_ingest_at(),
             ISSUE_INGEST_AMBER_SECONDS, ISSUE_INGEST_RED_SECONDS),
            ("alert ingest", _alert_ingest_at(),
             ALERT_INGEST_AMBER_SECONDS, ALERT_INGEST_RED_SECONDS)):
        item = _ingest_item(label, at, amber, red, now)
        if item:
            items.append(item)

    severity_order = {"red": 0, "amber": 1}
    items.sort(key=lambda i: (severity_order[i["severity"]], i["kind"], i["host"] or ""))
    return {"items": items,
            "stalled": _stalled(health_hosts, st.load_verify_worker(),
                                st.load_fix_worker())}


def cached_status() -> dict:
    """`status()` behind a short shared cache, for the per-tab poll."""
    global _cached
    with _cache_lock:
        mono = time.monotonic()
        if _cached and mono - _cached[0] < CACHE_SECONDS:
            return _cached[1]
        result = status()
        _cached = (mono, result)
        return result
