"""Keeping the stored author responses current without an operator's click.

A worker machine runs `responses.scan` on a cadence, under a lease so one
machine sweeps at a time, and skips a pass another machine finished within
INTERVAL_SECONDS. A pass reads only what changed since the last one (`since`);
with no pass on record it reads everything. Each pass is booked in the runs
ledger as PHASE.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta, timezone

from pipeline import settings, storekit
from prospector_app.backend import data, responses, schema_gate, worker_loops

PHASE = "responses:scan"
INTERVAL_SECONDS = 30 * 60
LEASE = "responses-sweep"
# Longer than any pass, so a lease outlives its holder's pass only when the
# holder died mid-pass.
LEASE_SECONDS = 2 * 3600
# GitHub's update times and ours are different clocks.
OVERLAP = timedelta(minutes=5)

_thread: threading.Thread | None = None
_stop = threading.Event()


def enabled() -> bool:
    """Whether this machine sweeps: a verify or fix worker with the switch on."""
    return settings.responses_sweep() and (settings.verify_worker_enabled()
                                           or settings.fix_worker_enabled())


def since(last: storekit.PhaseRun | None) -> str | None:
    """Where a pass after `last` lists from: `last`'s start when it read
    everything it asked for, else where `last` itself listed from, so an item
    whose read failed is asked for again. None reads everything."""
    if last is None:
        return None
    stats = last.raw.get("stats") or {}
    if stats.get("failed"):
        return stats.get("listed_from")
    start = storekit.parse_ts(last.started)
    if start is None:
        return None
    return (start - OVERLAP).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sweep_once(now: datetime | None = None) -> dict | None:
    """One pass. Returns the scan's result, or None when another pass is too
    recent or another machine holds the lease."""
    now = now or datetime.now(timezone.utc)
    last = data.latest_run(PHASE)
    if isinstance(last, storekit.PhaseRun):
        age = storekit.seconds_since(last.finished or last.started, now)
        if age is not None and age < INTERVAL_SECONDS:
            return None
    host = settings.worker_id()
    store = data.store()
    if not store.claim_lease(LEASE, host=host, seconds=LEASE_SECONDS):
        return None
    try:
        started = storekit.now()
        listed_from = since(last if isinstance(last, storekit.PhaseRun) else None)
        res = responses.scan(since=listed_from)
        store.append_run({"phase": PHASE, "started": started, "finished": storekit.now(),
                          "trigger": "worker",
                          "stats": {"checked": res["checked"],
                                    "with_response": res["with_response"],
                                    "failed": len(res["failed"]) + len(res["failed_issues"]),
                                    "listed_from": listed_from}})
    finally:
        store.release_lease(LEASE, host=host)
    return res


def _loop() -> None:
    while not _stop.is_set():
        try:
            if not schema_gate.paused("responses"):
                sweep_once()
        except Exception:
            traceback.print_exc()
        worker_loops.ran("responses", INTERVAL_SECONDS)
        _stop.wait(INTERVAL_SECONDS)


def start() -> bool:
    """Start the cadence on a sweeping machine (`enabled`). Idempotent."""
    global _thread
    if not enabled():
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="responses-sweep")
    _thread.start()
    return True
