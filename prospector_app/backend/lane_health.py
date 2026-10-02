"""What the two workers call to keep their lane health current.

Every ending goes through `note_failure` or `note_success`; the drain loops ask
`open_or_retest` before each pick. The policy is pipeline/worker_health.py;
the escalation is prospector_app/backend/escalation.py; this is the glue that
knows this machine's worker id and runs the self-test.
"""
from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone

from pipeline import capacity, headless_agent, settings, storekit, worker_health
from prospector_app.backend import data, escalation, worker_selftest

def enabled_lanes() -> tuple[str, ...]:
    """The lanes this machine runs: `security` and `verify` on a verify
    worker, `fix` on an autofix worker, `issue-fix` on an issue-fix worker,
    `cluster` on a clustering worker."""
    lanes: list[str] = []
    if settings.verify_worker_enabled():
        lanes += ["security", "verify"]
    if settings.fix_worker_enabled():
        lanes.append("fix")
    if settings.issue_fix_worker_enabled():
        lanes.append("issue-fix")
    if settings.cluster_worker_enabled():
        lanes.append("cluster")
    return tuple(lanes)


def _update(fn: Callable[[dict], object]) -> dict:
    return worker_health.update(data.store(), settings.worker_id(), fn)


def note_failure(lane: str, *, kind: str, reason: str, pr: int | None = None) -> None:
    """One machine-fault ending on `lane`. Escalates when it trips the lane.
    A service overload or a spent usage limit books nothing: neither is this
    machine's fault nor the work's. Best-effort: bookkeeping must not cost the
    caller its own ending."""
    if headless_agent.transient(reason) or headless_agent.limit_spent(reason):
        return
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
    escalate the outage once. A spent usage limit trips nothing — the run that
    hit it paused the account's unattended work until the reset, and the
    capacity gate holds the lanes until then."""
    if headless_agent.limit_spent(reason) or _account_paused():
        print(f"[capacity] usage limit reached; unattended agent work waits for the "
              f"reset: {reason}", flush=True)
        return
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


def _account_paused() -> bool:
    """Whether this machine's account has a pause running: a run that hit the
    usage limit recorded one, whatever its caller made of the error."""
    acct = capacity.account()
    if acct is None:
        return False
    try:
        pause = data.store().load_capacity(acct.key).get("pause") or {}
    except Exception:
        return False
    until = storekit.parse_ts(pause.get("until"))
    return until is not None and until > datetime.now(timezone.utc)


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
    note_account()
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


# How long one capacity decision answers the worker lanes' repeated asks.
CAPACITY_TTL_SECONDS = 60.0
# One decision for the machine's account, shared by its lanes, taken by one
# thread at a time so concurrent lanes never probe twice.
_decide_lock = threading.Lock()
_capacity_cache: dict[str, tuple[float, int, capacity.Decision]] = {}
_capacity_said: dict[str, bool] = {}
_account_noted: list[str] = []


def capacity_open(lane: str) -> bool:
    """Whether `lane` may start unattended agent work now under this machine's
    AI account's capacity policy. Asked only with an unattended item in hand,
    so an idle machine never spends a probe; a decision answers every lane for
    CAPACITY_TTL_SECONDS or until a pause is recorded, and each lane's change
    of answer is logged once."""
    with _decide_lock:
        now = time.monotonic()
        cached = _capacity_cache.get("account")
        if (cached is not None and now - cached[0] < CAPACITY_TTL_SECONDS
                and cached[1] == capacity.pause_generation()):
            decision = cached[2]
        else:
            acct = capacity.account()
            try:
                decision = capacity.check(data.store(), acct, probe=headless_agent.probe_reading)
            except Exception as e:
                decision = capacity.Decision(False, f"the capacity check failed: {e}", None)
            _capacity_cache["account"] = (now, capacity.pause_generation(), decision)
        changed = _capacity_said.get(lane) != decision.allowed
        _capacity_said[lane] = decision.allowed
    if changed:
        when = f" (retry ~{decision.retry_at:%H:%M} UTC)" if decision.retry_at else ""
        state = "open" if decision.allowed else "paused"
        print(f"[{lane}] unattended AI work {state}: {decision.reason}{when}", flush=True)
    return decision.allowed


def note_account() -> None:
    """Stamp this machine's AI account on its worker-health record once per
    account, so the roster shows which machines share one."""
    acct = capacity.account()
    if acct is None or _account_noted == [acct.key]:
        return
    try:
        _update(lambda r: r.__setitem__("ai_account", {
            "key": acct.key, "label": acct.label, "billing": acct.billing}))
        _account_noted[:] = [acct.key]
    except Exception:
        traceback.print_exc()
