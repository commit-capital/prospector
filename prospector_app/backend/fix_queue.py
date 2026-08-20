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


def queue_pr(n: int, action: str, source: str | None = None,
             guidance: str | None = None) -> dict:
    """Mark PR `n` queued for `action`. Raises ValueError with the
    operator-readable reason when the pre-check refuses. `source` stamps who
    queued it ("auto" for the idle hunter; the operator path passes None).

    `guidance` is the operator's own instruction for a `fix`: it becomes the
    agent's goal, and it is the authorization for the action where the profile
    names no fixable gates. Blank text is no instruction, so it is dropped
    rather than stored as an empty mandate."""
    if action not in settings.FIX_ACTIONS:
        raise ValueError(f"unknown autofix action {action!r}; valid actions are "
                         f"{', '.join(settings.FIX_ACTIONS)}")
    guidance = (guidance or "").strip() or None
    rec = data.store().load_pr(n)
    if rec is None:
        raise ValueError(f"PR #{n} not in store")
    ok, why = gates.fix_eligibility(rec, action, service.changed_paths(rec),
                                    guided=guidance is not None)
    if not ok:
        raise ValueError(f"PR #{n} is not eligible for {action}: {why}")
    status = (rec.fix_request or {}).get("status")
    if status in IN_FLIGHT:
        raise ValueError(f"PR #{n} already has a {status} fix request")
    rec.record_fix_request("queued", action, queued_at=_now(), source=source,
                           guidance=guidance, head_sha=rec.head_sha)
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
    request can be approved; a worker picks the approval up on its next tick and
    rebuilds the tree before pushing. The approval is recorded here rather than
    pushing inline, so an operator can approve from any machine while the push
    stays on one holding the push key.

    The authoring machine is carried across: a `resolve` holds its merge commit
    in that machine's worktree, so only it can push one."""
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
                           guidance=req.get("guidance"), host=req.get("host"),
                           head_sha=req.get("against_head_sha"))
    data.refresh()
    return {"pr": n, "status": "approved"}


# Listing order. `awaiting-review` leads because it is the only status an
# operator can act on; everything else is work in progress they are waiting out.
_QUEUE_STATUS_ORDER = {"awaiting-review": 0, "pushing": 1, "running": 2,
                       "approved": 3, "queued": 4}


def queue_entries() -> list[dict]:
    """Every PR with an autofix request in flight, the reviewable ones first and
    oldest-queued first within each status.

    `resolvable` is the claim the row makes: the action produced a change and
    the compile preflight did not reject it. A deployment configuring no
    verify.compile_cmd records None, which is not a failure — the merge or
    rebase resolving is the claim, and the build check corroborates it."""
    out: list[dict] = []
    for n, rec in data.prs().items():
        req = rec.fix_request or {}
        status = req.get("status")
        if status not in _QUEUE_STATUS_ORDER:
            continue
        pf = (req.get("result") or {}).get("compile_preflight")
        out.append({
            "pr": n, "title": rec.title, "status": status,
            "action": req.get("action"), "source": req.get("source"),
            "queued_at": req.get("queued_at"), "base_sha": req.get("base_sha"),
            "resolvable": status == "awaiting-review" and (pf is None or pf.get("exit") == 0),
        })
    out.sort(key=lambda e: (_QUEUE_STATUS_ORDER[e["status"]], str(e["queued_at"] or "")))
    return out


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
    STALE_BEAT_SECONDS. `push_identity` says whether THIS backend holds the
    credential.

    `can_queue` is what the app gates the buttons on, and it is deliberately
    NOT this backend's own configuration. The queue lives in the shared store
    precisely so an operator clicks from a laptop and the machine holding the
    key does the work — so an online worker anywhere is what makes queueing
    meaningful. A worker only starts once its key passes
    fix_worker.key_safety_failure, so a beating worker already proves a usable
    push identity exists."""
    from prospector_app.backend import fix_worker
    records = worker_records(data.store().load_fix_worker())
    hosts = [{"host": r.get("host"), "online": _beat_online(r.get("last_beat")),
              "last_beat": r.get("last_beat"), "current_pr": r.get("current_pr"),
              "autohunt": bool(r.get("autohunt"))} for r in records]
    online = any(h["online"] for h in hosts)
    fresh: dict = hosts[0] if hosts else {}
    return {"configured": fix_worker.enabled(),
            "online": online,
            "can_queue": online or settings.push_identity_configured(),
            "push_identity": settings.push_identity_configured(),
            "push_login": settings.PUSH_LOGIN or None,
            "autopush": sorted(settings.fix_autopush()),
            "host": fresh.get("host"), "current_pr": fresh.get("current_pr"),
            "last_beat": fresh.get("last_beat"), "hosts": hosts}
