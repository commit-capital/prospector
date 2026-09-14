"""Per-worker, per-lane health: the ONE policy for when a worker stops picking
work because the machine, not the PRs, is failing.

Every ending a worker writes is either the PR's (a refusal, a verdict, a push)
or the machine's (a sandbox that would not start, an agent that could not run,
a crash). The machine's endings are counted here per lane, and a run of them
trips the lane: it picks nothing until a self-test passes or an operator
resumes it, and the trip is escalated (a ledger entry, a banner, an issue).

The record is one registry row per worker (`Store.load_worker_health`), with
one entry per lane in LANES:

    {"consecutive_failures": int, "recent": [failure, ...],
     "last_success_at": iso | None,
     "tripped": {"at", "kind", "reason"} | None,
     "retest": {"at", "ok", "detail"} | None,
     "issue": {"signature", "number", "url", "filed_at"} | None}

Functions here are pure over that record; `update` is the one write path.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pipeline.storekit import now as _now

if TYPE_CHECKING:
    from pipeline.store import Store

LANES = ("security", "verify", "fix")

# Consecutive machine-fault endings that trip a lane.
TRIP_AFTER = 3
# How long a tripped lane waits between self-tests.
RETEST_SECONDS = 15 * 60
# How long one failure signature suppresses a second issue.
ISSUE_DEDUP_SECONDS = 7 * 24 * 3600
# How stale a worker's heartbeat is before its silence is escalated.
OFFLINE_AFTER_SECONDS = 3600
# Failures kept on the record for the operator and the issue body.
RECENT_KEEP = 5

_lock = threading.Lock()


def empty(host: str) -> dict:
    return {"host": host, "lanes": {}, "updated_at": None}


def lane(rec: dict, name: str) -> dict:
    """The lane's entry, created empty on first touch."""
    lanes = rec.setdefault("lanes", {})
    return lanes.setdefault(name, {
        "consecutive_failures": 0, "recent": [], "last_success_at": None,
        "tripped": None, "retest": None, "issue": None})


def is_tripped(rec: dict, name: str) -> bool:
    return bool(((rec.get("lanes") or {}).get(name) or {}).get("tripped"))


def record_failure(rec: dict, name: str, *, kind: str, reason: str,
                   pr: int | None = None, now: str | None = None) -> bool:
    """Count one machine-fault ending. Returns True when this one trips the
    lane (an already-tripped lane stays tripped and returns False)."""
    now = now or _now()
    entry = lane(rec, name)
    entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
    recent = list(entry.get("recent") or [])
    recent.append({"at": now, "kind": kind, "reason": reason[:600], "pr": pr})
    entry["recent"] = recent[-RECENT_KEEP:]
    rec["updated_at"] = now
    if entry.get("tripped") or entry["consecutive_failures"] < TRIP_AFTER:
        return False
    entry["tripped"] = {"at": now, "kind": kind,
                        "reason": f"{entry['consecutive_failures']} machine failures in a row; "
                                  f"last: {reason[:300]}"}
    return True


def record_success(rec: dict, name: str, *, now: str | None = None) -> None:
    """One ending that was the PR's, not the machine's: the failure run ends."""
    now = now or _now()
    entry = lane(rec, name)
    entry["consecutive_failures"] = 0
    entry["last_success_at"] = now
    rec["updated_at"] = now


def trip(rec: dict, name: str, *, kind: str, reason: str,
         now: str | None = None) -> bool:
    """Trip the lane at once, for a condition that needs no run to prove it
    (an agent outage, a failed self-test, a pin that will not refresh).
    Returns True when the lane was open."""
    now = now or _now()
    entry = lane(rec, name)
    rec["updated_at"] = now
    if entry.get("tripped"):
        return False
    entry["tripped"] = {"at": now, "kind": kind, "reason": reason[:600]}
    return True


def reopen(rec: dict, name: str, *, by: str, now: str | None = None) -> None:
    """Open the lane again: a passed self-test or an operator's Resume."""
    now = now or _now()
    entry = lane(rec, name)
    entry["tripped"] = None
    entry["consecutive_failures"] = 0
    entry["retest"] = {"at": now, "ok": True, "detail": by}
    rec["updated_at"] = now


def retest_due(rec: dict, name: str, now: datetime | None = None) -> bool:
    """Whether a tripped lane should run its self-test: never retested since
    the trip, or RETEST_SECONDS past the last one."""
    entry = lane(rec, name)
    tripped = entry.get("tripped")
    if not tripped:
        return False
    now = now or datetime.now(timezone.utc)
    last = (entry.get("retest") or {}).get("at") or tripped.get("at")
    try:
        at = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    if (entry.get("retest") or {}).get("at") is None:
        return True
    return now - at >= timedelta(seconds=RETEST_SECONDS)


def record_retest(rec: dict, name: str, *, ok: bool, detail: str,
                  now: str | None = None) -> None:
    now = now or _now()
    lane(rec, name)["retest"] = {"at": now, "ok": ok, "detail": detail[:600]}
    rec["updated_at"] = now


def signature(kind: str, reason: str) -> str:
    """One string per failure shape: the kind plus the reason with numbers,
    hashes, and paths flattened, so the same breakage files one issue."""
    text = re.sub(r"[0-9a-f]{7,}", "#", reason.lower())
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"/\S+", "/…", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f"{kind}: {text[:120]}"


def issue_due(rec: dict, name: str, sig: str, now: datetime | None = None) -> bool:
    """Whether a trip with this signature warrants a new issue: none filed for
    it, or the last one older than ISSUE_DEDUP_SECONDS."""
    issue = lane(rec, name).get("issue") or {}
    if issue.get("signature") != sig:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        filed = datetime.fromisoformat(str(issue.get("filed_at")))
    except ValueError:
        return True
    return now - filed >= timedelta(seconds=ISSUE_DEDUP_SECONDS)


def record_issue(rec: dict, name: str, *, sig: str, number: int | None,
                 url: str | None, now: str | None = None) -> None:
    lane(rec, name)["issue"] = {"signature": sig, "number": number, "url": url,
                                "filed_at": now or _now()}


def load(store: Store, host: str) -> dict:
    return dict((store.load_worker_health().get("hosts") or {}).get(host) or empty(host))


def update(store: Store, host: str, fn: Callable[[dict], object]) -> dict:
    """Load the worker's record, apply `fn` to it, save it. One lock covers the
    two lanes' threads inside one worker process; another machine's record is
    a different registry key."""
    with _lock:
        rec = load(store, host)
        fn(rec)
        store.save_worker_health(rec)
        return rec
