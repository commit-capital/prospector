"""Backend reconciliation for asynchronous external review re-triggers.

Posting Greptile's mention only starts its work.  This module keeps that
lifecycle in the backend: capture the currently visible scored summary before
the mention lands, poll until Greptile edits/posts a newer summary, then run the
canonical targeted ingest so the shared score, reviewed SHA, checks, and gates
all advance together.  The daemon task is independent of the browser that
initiated the action.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from pipeline import greptile
from pipeline import ingest
from app.backend import data

_log = logging.getLogger(__name__)

POLL_SECONDS = float(os.environ.get("COCKPIT_REVIEW_REFRESH_POLL_SECONDS", "10"))
POLL_ATTEMPTS = int(os.environ.get("COCKPIT_REVIEW_REFRESH_ATTEMPTS", "30"))


@dataclass(frozen=True)
class Baseline:
    version: str | None
    previous_score: int | None
    captured_at: datetime


def capture(n: int) -> Baseline:
    """Capture the scored summary immediately before posting the trigger."""
    rec = data.prs().get(int(n))
    feedback = greptile.fetch_greptile_feedback(n)
    return Baseline(
        version=(feedback or {}).get("version"),
        previous_score=rec.review_score if rec is not None else None,
        captured_at=datetime.now(timezone.utc),
    )


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_new(feedback: dict | None, baseline: Baseline) -> bool:
    if feedback is None:
        return False
    if baseline.version is not None:
        return feedback.get("version") != baseline.version
    if baseline.previous_score is None:
        # The PR had no stored score and no scored summary before the trigger;
        # the first one to appear is necessarily the result we are waiting for.
        return True
    # Capturing the live baseline failed even though the store had an older
    # score.  Do not mistake that old summary for the new result when GitHub
    # becomes reachable again; require a summary updated after the trigger.
    updated = _timestamp(feedback.get("updated_at"))
    return updated is not None and updated >= baseline.captured_at


def wait_and_refresh(
    n: int,
    baseline: Baseline,
    *,
    attempts: int = POLL_ATTEMPTS,
    interval: float = POLL_SECONDS,
    keep_going: Callable[[], bool] = lambda: True,
) -> bool:
    """Wait for a new scored summary and ingest it. Returns whether it landed."""
    for attempt in range(attempts):
        if not keep_going():
            return False
        feedback = greptile.fetch_greptile_feedback(n)
        if _is_new(feedback, baseline):
            if not keep_going():
                return False
            try:
                ingest.refresh_prs(data.store(), [n])
                data.refresh()
                return True
            except Exception as exc:  # noqa: BLE001 — bounded background retry
                # The summary is ready but a transient GitHub/store read failed;
                # keep retrying within the same bounded window.
                _log.warning("review refresh for PR #%s failed: %s", n, exc)
        if attempt + 1 < attempts:
            time.sleep(interval)
    _log.warning("review refresh for PR #%s timed out after %s attempts", n, attempts)
    return False


_lock = threading.Lock()
_generation: dict[int, int] = {}


def schedule(n: int, baseline: Baseline) -> None:
    """Start/replace the backend wait task for one re-triggered PR."""
    n = int(n)
    with _lock:
        generation = _generation.get(n, 0) + 1
        _generation[n] = generation

    def current() -> bool:
        with _lock:
            return _generation.get(n) == generation

    def run() -> None:
        try:
            wait_and_refresh(n, baseline, keep_going=current)
        finally:
            with _lock:
                if _generation.get(n) == generation:
                    _generation.pop(n, None)

    threading.Thread(target=run, daemon=True, name=f"review-refresh-{n}").start()
