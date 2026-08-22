"""Wait for one reviewer's fresh verdict after a re-trigger, then ingest it.

A re-trigger posts the reviewer's mention on the PR; the bot then re-reviews
the current head and leaves a new verdict. This module captures the reviewer's
entry version right before the post, polls the live feed for a different
version, and targeted-ingests the PR so the shared snapshot carries the new
verdict without waiting for the next scheduled ingest."""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from pipeline import ingest, review_fetch, reviewers
from prospector_app.backend import data

_log = logging.getLogger(__name__)

POLL_SECONDS = float(os.environ.get("PROSPECTOR_REVIEW_REFRESH_POLL_SECONDS", "10"))
POLL_ATTEMPTS = int(os.environ.get("PROSPECTOR_REVIEW_REFRESH_ATTEMPTS", "30"))


@dataclass(frozen=True)
class Baseline:
    version: str | None
    captured_at: datetime


def live_entry(n: int, reviewer_id: str) -> dict | None:
    """The reviewer's entry parsed from the PR's live feed, or None when the bot
    has left nothing (or GitHub is unreachable)."""
    reviewer = reviewers.REVIEWERS.get(reviewer_id)
    if reviewer is None:
        return None
    feed = review_fetch.fetch_feeds([int(n)]).get(int(n))
    if feed is None:
        return None
    rec = data.prs().get(int(n))
    previous = rec.review_entry(reviewer_id) if rec is not None else None
    return reviewers.parse(reviewer, feed, feed.head_sha, previous)


def capture(n: int, reviewer_id: str) -> Baseline:
    """Capture the reviewer's entry version immediately before posting the trigger."""
    reviewer = reviewers.REVIEWERS.get(reviewer_id)
    entry = live_entry(n, reviewer_id)
    version = reviewers.version(reviewer, entry) if reviewer is not None else None
    return Baseline(version=version, captured_at=datetime.now(timezone.utc))


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_new(entry: dict | None, reviewer_id: str, baseline: Baseline) -> bool:
    if entry is None:
        return False
    reviewer = reviewers.REVIEWERS.get(reviewer_id)
    if reviewer is None:
        return False
    if baseline.version is not None:
        return reviewers.version(reviewer, entry) != baseline.version
    # No entry existed before the trigger: the first one to appear is the
    # result, unless it is older than the trigger (a baseline capture that
    # failed while the bot had already posted).
    observed = _timestamp(entry.get("observed_at"))
    return observed is None or observed >= baseline.captured_at


def wait_and_refresh(
    n: int,
    reviewer_id: str,
    baseline: Baseline,
    *,
    attempts: int = POLL_ATTEMPTS,
    interval: float = POLL_SECONDS,
    keep_going: Callable[[], bool] = lambda: True,
) -> bool:
    """Wait for the reviewer's new verdict and ingest it. Returns whether it landed."""
    for attempt in range(attempts):
        if not keep_going():
            return False
        entry = live_entry(n, reviewer_id)
        if _is_new(entry, reviewer_id, baseline):
            if not keep_going():
                return False
            try:
                ingest.refresh_prs(data.store(), [n])
                data.refresh()
                return True
            except Exception as exc:  # noqa: BLE001 — bounded background retry
                # The verdict is ready but a transient GitHub/store read failed;
                # keep retrying within the same bounded window.
                _log.warning("review refresh for PR #%s failed: %s", n, exc)
        if attempt + 1 < attempts:
            time.sleep(interval)
    _log.warning("review refresh for PR #%s (%s) timed out after %s attempts",
                 n, reviewer_id, attempts)
    return False


_lock = threading.Lock()
_generation: dict[tuple[int, str], int] = {}


def schedule(n: int, reviewer_id: str, baseline: Baseline) -> None:
    """Start/replace the backend wait task for one re-triggered PR and reviewer."""
    key = (int(n), reviewer_id)
    with _lock:
        generation = _generation.get(key, 0) + 1
        _generation[key] = generation

    def current() -> bool:
        with _lock:
            return _generation.get(key) == generation

    def run() -> None:
        try:
            wait_and_refresh(key[0], reviewer_id, baseline, keep_going=current)
        finally:
            with _lock:
                if _generation.get(key) == generation:
                    _generation.pop(key, None)

    threading.Thread(target=run, daemon=True,
                     name=f"review-refresh-{key[0]}-{reviewer_id}").start()
