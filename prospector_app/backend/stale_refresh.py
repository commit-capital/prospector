"""Refreshing the merge candidates whose facts went stale.

A merge-disposition PR whose head moved reads as not clean until INGEST
re-stamps its signals, reviews, and drift at the new head, and nothing else
re-runs INGEST for it. A worker machine refreshes those PRs on a cadence, a
bounded batch at a time, through the same `ingest.refresh_prs` the single-PR
re-run uses, so a candidate never waits on a human to notice it went stale.
"""
from __future__ import annotations

import threading
import traceback

from pipeline import ingest, settings
from pipeline.freshness import is_current
from prospector_app.backend import data

REFRESH_SECONDS = 30 * 60
# PRs refreshed per pass; each costs a handful of GitHub reads.
BATCH = 40

_thread: threading.Thread | None = None
_stop = threading.Event()


def stale_merge_candidates() -> list[int]:
    """Open merge-disposition PRs with a stale or missing signals, reviews, or
    drift section, lowest number first."""
    out: list[int] = []
    for n, pr in sorted(data.prs().items()):
        if pr.state != "open" or pr.disposition != "merge":
            continue
        if not all(is_current(pr, section) for section in ("signals", "reviews", "drift")):
            out.append(n)
    return out


def refresh_stale(limit: int = BATCH) -> list[int]:
    """Refresh up to `limit` stale merge candidates. Returns the PRs refreshed."""
    numbers = stale_merge_candidates()[:limit]
    if not numbers:
        return []
    ingest.refresh_prs(data.store(), numbers)
    data.refresh()
    print(f"[stale-refresh] re-ingested {len(numbers)} stale merge candidate(s): "
          f"{numbers[:12]}{' …' if len(numbers) > 12 else ''}", flush=True)
    return numbers


def _loop() -> None:
    while not _stop.is_set():
        try:
            refresh_stale()
        except Exception:
            traceback.print_exc()
        _stop.wait(REFRESH_SECONDS)


def start() -> bool:
    """Start the cadence on a worker machine (one whose verify or fix lane is
    enabled), so one machine per deployment does the refreshing. Idempotent."""
    global _thread
    if not (settings.verify_worker_enabled() or settings.fix_worker_enabled()):
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="stale-refresh")
    _thread.start()
    return True
