"""What the two workers call to keep their lane health current.

Every ending goes through `note_failure` or `note_success`; the drain loops ask
`open_or_retest` before each pick. The policy is pipeline/worker_health.py;
the escalation is prospector_app/backend/escalation.py; this is the glue that
knows this machine's worker id and runs the self-test.
"""
from __future__ import annotations

import traceback
from collections.abc import Callable

from pipeline import settings, worker_health
from prospector_app.backend import data, escalation, worker_selftest

def enabled_lanes() -> tuple[str, ...]:
    """The lanes this machine runs: `security` and `verify` on a verify
    worker, `fix` on an autofix worker."""
    lanes: list[str] = []
    if settings.verify_worker_enabled():
        lanes += ["security", "verify"]
    if settings.fix_worker_enabled():
        lanes.append("fix")
    return tuple(lanes)


def _update(fn: Callable[[dict], object]) -> dict:
    return worker_health.update(data.store(), settings.worker_id(), fn)


def note_failure(lane: str, *, kind: str, reason: str, pr: int | None = None) -> None:
    """One machine-fault ending on `lane`. Escalates when it trips the lane.
    Best-effort: bookkeeping must not cost the caller its own ending."""
    try:
        tripped: list[bool] = []
        _update(lambda r: tripped.append(worker_health.record_failure(
            r, lane, kind=kind, reason=reason, pr=pr)))
        if tripped and tripped[0]:
            escalation.escalate_trip([lane])
    except Exception:
        traceback.print_exc()


def note_success(lane: str) -> None:
    try:
        _update(lambda r: worker_health.record_success(r, lane))
    except Exception:
        traceback.print_exc()


def trip_agent_lanes(reason: str) -> None:
    """The agent CLI cannot run: close every lane this machine runs, now, and
    escalate the outage once."""
    try:
        newly: list[str] = []

        def _trip(rec: dict) -> None:
            for lane in enabled_lanes():
                if worker_health.trip(rec, lane, kind="agent-unavailable", reason=reason):
                    newly.append(lane)
        _update(_trip)
        if newly:
            escalation.escalate_trip(newly)
    except Exception:
        traceback.print_exc()


def trip_lane(lane: str, *, kind: str, reason: str) -> None:
    """Trip one lane on a condition that needs no run to prove it."""
    try:
        newly: list[bool] = []
        _update(lambda r: newly.append(worker_health.trip(r, lane, kind=kind, reason=reason)))
        if newly and newly[0]:
            escalation.escalate_trip([lane])
    except Exception:
        traceback.print_exc()


def trip_kind(lane: str) -> str | None:
    """The kind `lane` is tripped on, or None when it is open."""
    rec = worker_health.load(data.store(), settings.worker_id())
    tripped = worker_health.lane(rec, lane).get("tripped") or {}
    return str(tripped.get("kind") or "unknown") if tripped else None


def open_or_retest(lane: str) -> bool:
    """Whether `lane` may pick work now. A tripped lane whose retest is due
    runs the self-test for its trip kind and reopens on a pass; a lane tripped
    on a kind no probe answers reopens once after the cool-down; a tripped
    lane between retests answers False without doing anything."""
    try:
        rec = worker_health.load(data.store(), settings.worker_id())
        if not worker_health.is_tripped(rec, lane):
            return True
        kind = str((worker_health.lane(rec, lane).get("tripped") or {}).get("kind") or "")
        if not worker_selftest.testable(kind):
            if worker_health.cooled(rec, lane):
                _update(lambda r: worker_health.reopen(
                    r, lane, by="cooled down; opened to see whether the cause passed"))
                print(f"[worker-health] {lane} lane reopened on {settings.worker_id()} "
                      f"after the cool-down", flush=True)
                return True
            return False
        if not worker_health.retest_due(rec, lane):
            return False
        why = worker_selftest.run(kind)
        if why is None:
            _update(lambda r: worker_health.reopen(r, lane, by="self-test passed"))
            print(f"[worker-health] {lane} lane reopened on {settings.worker_id()}: "
                  f"self-test passed", flush=True)
            return True
        _update(lambda r: worker_health.record_retest(r, lane, ok=False, detail=why))
        print(f"[worker-health] {lane} lane stays closed on {settings.worker_id()}: {why}",
              flush=True)
        return False
    except Exception:
        traceback.print_exc()
        return True
