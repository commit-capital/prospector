"""The autofix queue, over the store's per-PR fix_request section. Any app
queues, cancels, and approves here; the fix worker (fix_worker.py) on the
machine holding the push identity picks queued requests up and acts on the
contributor's branch. State lives in the shared store, so a click on one machine
reaches the runner on another, and survives restarts.

Queueing pre-checks only what this machine can know cheaply: the PR exists, the
autofix gate allows the action, and no request is already in flight. The
authoritative checks are the runner's — the live PR's state, its "Allow edits
from maintainers" grant, the head SHA the request was pinned against, and the
compile preflight — all of which fail closed there.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pipeline import gates, settings
from pipeline.storekit import now as _now
from prospector_app.backend import data, service

# A worker heartbeat older than this reads as offline: the worker beats every
# poll tick (fix_worker.POLL_SECONDS), so several missed beats mean the process
# is gone, not slow.
STALE_BEAT_SECONDS = 90.0

# Statuses that mean a request is still working its way through the queue, so a
# second request for the same PR would race it.
IN_FLIGHT = ("queued", "running", "awaiting-review", "approved", "pushing")


def queue_pr(n: int, action: str, source: str | None = None) -> dict:
    """Mark PR `n` queued for `action`. Raises ValueError with the
    operator-readable reason when the pre-check refuses. `source` stamps who
    queued it ("auto" for the idle hunter; the operator path passes None)."""
    if action not in settings.FIX_ACTIONS:
        raise ValueError(f"unknown autofix action {action!r}; valid actions are "
                         f"{', '.join(settings.FIX_ACTIONS)}")
    rec = data.store().load_pr(n)
    if rec is None:
        raise ValueError(f"PR #{n} not in store")
    ok, why = gates.fix_eligibility(rec, action, service.changed_paths(rec))
    if not ok:
        raise ValueError(f"PR #{n} is not eligible for {action}: {why}")
    status = (rec.fix_request or {}).get("status")
    if status in IN_FLIGHT:
        raise ValueError(f"PR #{n} already has a {status} fix request")
    rec.record_fix_request("queued", action, queued_at=_now(), source=source,
                           head_sha=rec.head_sha)
    data.refresh()
    return {"pr": n, "action": action, "status": "queued"}


def dequeue_pr(n: int) -> dict:
    """Cancel PR `n`'s fix request. Only a request the runner has not started —
    `queued` — or one parked for review can be cancelled; a running action is
    not interrupted from the app. Cancelling an `awaiting-review` request is how
    an operator discards an authored fix they do not want pushed."""
    rec = data.store().load_pr(n)
    if rec is None:
        raise ValueError(f"PR #{n} not in store")
    req = rec.fix_request or {}
    if req.get("status") not in ("queued", "awaiting-review"):
        raise ValueError(f"PR #{n} has no cancellable fix request "
                         f"(status: {req.get('status') or 'none'})")
    rec.record_fix_request("cancelled", req.get("action", "fix"),
                           queued_at=req.get("queued_at"), finished_at=_now(),
                           source=req.get("source"))
    data.refresh()
    return {"pr": n, "status": "cancelled"}


def approve_pr(n: int) -> dict:
    """Approve PR `n`'s authored fix for pushing. Only an `awaiting-review`
    request can be approved; the worker picks the approval up on its next tick
    and pushes from the worktree it already prepared. The approval is recorded
    here rather than pushing inline, so an operator can approve from any machine
    while the push stays on the one holding the key."""
    rec = data.store().load_pr(n)
    if rec is None:
        raise ValueError(f"PR #{n} not in store")
    req = rec.fix_request or {}
    if req.get("status") != "awaiting-review":
        raise ValueError(f"PR #{n} has no fix awaiting review "
                         f"(status: {req.get('status') or 'none'})")
    if rec.head_sha != (req.get("against_head_sha") or rec.head_sha):
        raise ValueError(f"PR #{n}'s head moved since the fix was authored — "
                         "re-queue so the change applies to the author's latest head")
    rec.record_fix_request("approved", req.get("action", "fix"),
                           queued_at=req.get("queued_at"), source=req.get("source"),
                           base_sha=req.get("base_sha"), result=req.get("result"),
                           head_sha=req.get("against_head_sha"))
    data.refresh()
    return {"pr": n, "status": "approved"}


def _beat_online(last: object) -> bool:
    """Whether a heartbeat stamp is within STALE_BEAT_SECONDS."""
    if not isinstance(last, str):
        return False
    try:
        beat = datetime.fromisoformat(last)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - beat).total_seconds() < STALE_BEAT_SECONDS


def worker_records(reg: dict) -> list[dict]:
    """The fix_worker registry's per-host heartbeat records, freshest first."""
    hosts = reg.get("hosts") or {}
    return sorted(hosts.values(),
                  key=lambda r: str(r.get("last_beat") or ""), reverse=True)


def runner_status() -> dict:
    """Whether a fix worker is alive against this store: `configured` says this
    backend runs one, `online` says some machine's worker beat within
    STALE_BEAT_SECONDS (the queue is shared, so the runner may be elsewhere).
    `push_identity` says whether THIS backend could push at all, which is what
    the app renders the buttons' disabled reason from. `autopush` names the
    actions this backend pushes without review."""
    from prospector_app.backend import fix_worker
    records = worker_records(data.store().load_fix_worker())
    hosts = [{"host": r.get("host"), "online": _beat_online(r.get("last_beat")),
              "last_beat": r.get("last_beat"), "current_pr": r.get("current_pr"),
              "autohunt": bool(r.get("autohunt"))} for r in records]
    fresh: dict = hosts[0] if hosts else {}
    return {"configured": fix_worker.enabled(),
            "online": any(h["online"] for h in hosts),
            "push_identity": settings.push_identity_configured(),
            "push_login": settings.PUSH_LOGIN or None,
            "autopush": sorted(settings.FIX_AUTOPUSH),
            "host": fresh.get("host"), "current_pr": fresh.get("current_pr"),
            "last_beat": fresh.get("last_beat"), "hosts": hosts}
