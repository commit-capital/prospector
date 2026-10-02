"""Watching GitHub for new PRs and pushed heads.

INGEST is a Control-tab job, so a PR opened, pushed to, reopened or closed
since the last one is not in the store, and nothing scans its code. A worker
machine lists the PRs GitHub reports updated since the last pass — the later
of the last watch pass and the last full ingest, less OVERLAP — records each
new arrival, moved head and reopen through ingest's own upsert and each close
on its record, then wakes `threat_refresh` so the new heads are scanned within
minutes. With no ingest on record a pass lists every open PR. A pass another
machine on the store finished within INTERVAL_SECONDS is not repeated, and
each pass is booked in the runs ledger as PHASE.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta, timezone

from pipeline import ingest, settings, storekit
from prospector_app.backend import data, threat_refresh

PHASE = "ingest:watch"
INTERVAL_SECONDS = 15 * 60
# How far back past the watermark a pass lists, so a PR updated while the last
# pass was running is not missed.
OVERLAP = timedelta(minutes=5)

_thread: threading.Thread | None = None
_stop = threading.Event()


def enabled() -> bool:
    """Whether this machine watches: a verify or fix worker with the switch on."""
    return settings.pr_watch() and (settings.verify_worker_enabled()
                                    or settings.fix_worker_enabled())


def since(watch_started: str | None, ingest_started: str | None) -> str | None:
    """The time a pass lists from, in GitHub's timestamp format, or None when
    neither a watch pass nor a full ingest is on record."""
    stamps = [t for t in (storekit.parse_ts(watch_started), storekit.parse_ts(ingest_started))
              if t is not None]
    if not stamps:
        return None
    return (max(stamps) - OVERLAP).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest(phase: str) -> storekit.PhaseRun | None:
    rec = data.latest_run(phase)
    return rec if isinstance(rec, storekit.PhaseRun) else None


def watch_once(now: datetime | None = None) -> dict[str, int] | None:
    """One pass. Returns its stats, or None when another pass is too recent."""
    now = now or datetime.now(timezone.utc)
    last = _latest(PHASE)
    if last is not None:
        age = storekit.seconds_since(last.finished or last.started, now)
        if age is not None and age < INTERVAL_SECONDS:
            return None
    started = storekit.now()
    full = _latest("ingest")
    mark = since(last.started if last else None, full.started if full else None)
    listed = ingest.fetch_updated_prs(mark) if mark else ingest.fetch_open_prs()
    data.refresh()
    store = data.store()
    changed = ingest.select_changed(listed, data.prs())
    upserted = ingest.upsert_listed(store, changed.upsert)["upserted"] if changed.upsert else 0
    closed = ingest.record_closed(store, changed.closed) if changed.closed else 0
    stats = {"listed": len(listed), "upserted": upserted, "closed": closed}
    store.append_run({"phase": PHASE, "started": started, "finished": storekit.now(),
                      "trigger": "worker", "stats": stats})
    data.refresh()
    if upserted:
        threat_refresh.wake()
    if upserted or closed:
        print(f"[pr-watch] recorded {upserted} new or pushed PR(s) and {closed} close(s) "
              f"from {len(listed)} listed", flush=True)
    return stats


def _loop() -> None:
    while not _stop.is_set():
        try:
            watch_once()
        except Exception:
            traceback.print_exc()
        _stop.wait(INTERVAL_SECONDS)


def start() -> bool:
    """Start the cadence on a watching machine (`enabled`). Idempotent."""
    global _thread
    if not enabled():
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="pr-watch")
    _thread.start()
    return True
