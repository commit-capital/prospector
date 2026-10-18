"""Keeping the stored author responses current without an operator's click.

A worker machine runs `responses.scan` on a cadence, under a lease so one
machine sweeps at a time, and skips a pass another machine finished within
INTERVAL_SECONDS. Each pass is booked in the runs ledger as PHASE.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone

from pipeline import settings, storekit
from prospector_app.backend import data, responses, schema_gate, worker_loops

PHASE = "responses:scan"
INTERVAL_SECONDS = 30 * 60
LEASE = "responses-sweep"
# Longer than any pass, so a lease outlives its holder's pass only when the
# holder died mid-pass.
LEASE_SECONDS = 2 * 3600

_thread: threading.Thread | None = None
_stop = threading.Event()


def enabled() -> bool:
    """Whether this machine sweeps: a verify or fix worker with the switch on."""
    return settings.responses_sweep() and (settings.verify_worker_enabled()
                                           or settings.fix_worker_enabled())


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
        res = responses.scan()
        store.append_run({"phase": PHASE, "started": started, "finished": storekit.now(),
                          "trigger": "worker",
                          "stats": {"checked": res["checked"],
                                    "with_response": res["with_response"],
                                    "failed": len(res["failed"]) + len(res["failed_issues"])}})
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
