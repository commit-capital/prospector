"""Writing the PR page's plain-language card unattended (pipeline/display_pass.py).

On a clustering worker (TRIAGE_CLUSTER_WORKER=1), every REFRESH_SECONDS one
machine at a time (LEASE) writes the card for up to PER_PASS open, analyzed
PRs without a current one, merge picks first, and a UTC day's passes at most
settings.display_daily(), counted from the ledger rows stamped TRIGGER. Every
agent call is gated by the AI capacity policy and booked as LANE's spend; a
closed gate or an agent outage ends the pass.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime

from pipeline import capacity, display_pass, headless_agent, settings, storekit
from prospector_app.backend import data, profile_gate, schema_gate, worker_loops

LANE = "display"
TRIGGER = "worker"
REFRESH_SECONDS = 30 * 60
PER_PASS = 10
LEASE = "display-lane"
LEASE_SECONDS = 2 * 3600

_thread: threading.Thread | None = None
_stop = threading.Event()


def used_today(now: datetime | None = None) -> int:
    midnight = storekit.utc_midnight(now)
    return sum(1 for r in data.store().runs(since=midnight.isoformat(), phase=display_pass.PHASE)
               if isinstance(r, storekit.PhaseRun) and r.raw.get("trigger") == TRIGGER)


def pass_once() -> list[int]:
    """Write the cards one pass may; the PRs written."""
    left = min(PER_PASS, settings.display_daily() - used_today())
    picks = display_pass.wanted(data.prs())[:max(0, left)]
    if not picks:
        return []
    store, host = data.store(), settings.worker_id()
    if not store.claim_lease(LEASE, host=host, seconds=LEASE_SECONDS):
        return []
    written: list[int] = []
    try:
        with capacity.unattended(LANE):
            for n in picks:
                if isinstance(display_pass.write(store, n, trigger=TRIGGER), dict):
                    written.append(n)
    except (capacity.CapacityPaused, headless_agent.AgentUnavailable) as e:
        print(f"[display] pass stopped: {e}", flush=True)
    finally:
        store.release_lease(LEASE, host=host)
        data.refresh()
    return written


def _loop() -> None:
    while not _stop.is_set():
        try:
            if not (schema_gate.paused(LANE) or profile_gate.paused(LANE)):
                pass_once()
        except Exception:
            traceback.print_exc()
        worker_loops.ran(LANE, REFRESH_SECONDS)
        _stop.wait(REFRESH_SECONDS)


def start() -> bool:
    """Start the cadence on a clustering worker. Idempotent."""
    global _thread
    if not settings.cluster_worker_enabled():
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="display-refresh")
    _thread.start()
    return True
