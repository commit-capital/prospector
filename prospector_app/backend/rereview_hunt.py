"""Asking a reviewer for the verdict a head never got.

A PR whose active reviewer's bar is stale or pending at its current head is
neither clean nor fixable: the hunter's `fix` refuses it because the verdict
describes a head the author moved past, or none yet, and nothing else asks
the reviewer again. A worker machine asks on a cadence: for an open,
mergeable, CI-green PR whose head is at least HEAD_AGE_SECONDS old, it posts
the reviewer's mention as the bot through `executor.retrigger_review`
(Activity-logged), once per PR and head, a bounded number per pass and per
UTC day, and starts the ingest wait for the verdict.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta, timezone

from pipeline import review_policy, reviewers, settings, storekit
from pipeline.freshness import is_current
from pipeline.reviewers import Reviewer
from pipeline.storekit import now as _now
from prospector_app.backend import data, executor, review_refresh

REFRESH_SECONDS = 30 * 60
# Mentions posted per pass, so one sweep never floods the repository.
PER_PASS = 5
# How old a head must be before its missing verdict is asked for: the
# reviewer's own push-triggered review has had this long to arrive.
HEAD_AGE_SECONDS = 6 * 3600
# The ledger phase a request is booked under; the booking is what keeps a head
# from being asked about twice.
PHASE = "rereview:request"
# How far back the ledger is read for earlier requests.
LOOKBACK_DAYS = 14

_thread: threading.Thread | None = None
_stop = threading.Event()


def _parse(stamp: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def candidates(now: datetime | None = None) -> list[tuple[int, Reviewer]]:
    """(PR, reviewer) pairs whose verdict is missing at the current head: open,
    mergeable, CI passing, signals current, head at least HEAD_AGE_SECONDS old,
    and the reviewer's bar stale or pending. Lowest PR number first."""
    now = now or datetime.now(timezone.utc)
    out: list[tuple[int, Reviewer]] = []
    askable = [r for r in review_policy.active_reviewers(reviewers.REVIEW) if r.retrigger_mention]
    if not askable:
        return out
    for n, pr in sorted(data.prs().items()):
        if pr.state != "open" or pr.mergeable is not True or pr.ci != "passing":
            continue
        if not is_current(pr, "signals"):
            continue
        updated = _parse(pr.updated_at)
        if updated is None or (now - updated).total_seconds() < HEAD_AGE_SECONDS:
            continue
        for r in askable:
            if review_policy.bar(pr, r).status in (reviewers.STALE, reviewers.PENDING):
                out.append((n, r))
    return out


def _requests(since: datetime) -> list[dict]:
    """The request bookings since `since`, as their raw ledger records."""
    rows = data.store().runs(since=since.isoformat())
    return [r.raw for r in rows if isinstance(r, storekit.PhaseRun) and r.phase == PHASE]


def requested(bookings: list[dict], n: int, head_sha: str | None) -> bool:
    return any(b.get("pr") == n and (b.get("stats") or {}).get("head_sha") == head_sha
               for b in bookings)


def used_today(bookings: list[dict], now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return sum(1 for b in bookings if str(b.get("finished") or "") >= midnight)


def request_rereviews(limit: int = PER_PASS) -> list[int]:
    """Post up to `limit` reviewer mentions for heads whose verdict is missing.
    Returns the PRs asked about. Nothing is posted without a mintable bot
    token, when the lane is off, or past the day's budget."""
    if not settings.fix_hunt_rereview():
        return []
    picks = candidates()
    if not picks:
        return []
    now = datetime.now(timezone.utc)
    bookings = _requests(now - timedelta(days=LOOKBACK_DAYS))
    budget = settings.rereview_budget() - used_today(bookings, now)
    if budget <= 0:
        return []
    token = executor.mint_bot_token()
    if not token:
        print("[rereview] this machine cannot mint the bot token, so it asks for no "
              "re-reviews", flush=True)
        return []
    asked: list[int] = []
    snap = data.prs()
    for n, r in picks:
        if len(asked) >= min(limit, budget):
            break
        head = snap[n].head_sha
        if requested(bookings, n, head):
            continue
        baseline = review_refresh.capture(n, r.id)
        res = executor.retrigger_review(n, r.id, token=token, dry_run=False)
        stamp = _now()
        data.store().append_run({
            "phase": PHASE, "pr": n, "started": stamp, "finished": stamp,
            "trigger": "autohunt",
            "stats": {"reviewer": r.id, "head_sha": head, "status": res.get("status")}})
        bookings.append({"pr": n, "finished": stamp, "stats": {"head_sha": head}})
        if res.get("status") == "executed":
            review_refresh.schedule(n, r.id, baseline)
            asked.append(n)
        print(f"[rereview] asked {r.label} for a verdict on PR #{n} at {str(head)[:12]}: "
              f"{res.get('status')}", flush=True)
    return asked


def _loop() -> None:
    while not _stop.is_set():
        try:
            request_rereviews()
        except Exception:
            traceback.print_exc()
        _stop.wait(REFRESH_SECONDS)


def start() -> bool:
    """Start the cadence on a machine whose fix worker hunts. Idempotent."""
    global _thread
    if not (settings.fix_worker_enabled() and settings.fix_autohunt()):
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="rereview-hunt")
    _thread.start()
    return True
