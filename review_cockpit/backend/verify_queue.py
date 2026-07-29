"""The operator's sandbox-verification queue, over the store's per-PR
verify_request section. Any cockpit queues (and cancels) here; the verification
worker (verify_worker.py) on the sandbox-capable machine picks queued PRs up
and runs pipeline/verify_pr.py. State lives in the shared store, so a queue
click on one machine reaches the runner on another, and survives restarts.

Queueing pre-checks only what this machine can know cheaply: the PR exists, is
open, is not threat-flagged, and has no request already in flight. The
authoritative safety floor (deps-touched, unreadable diff — both need the diff
cache) is the orchestrator's, which fails closed on the runner.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pipeline.storekit import now as _now
from review_cockpit.backend import data

# A worker heartbeat older than this reads as offline: the worker beats every
# poll tick (verify_worker.POLL_SECONDS), so several missed beats mean the
# process is gone, not slow.
STALE_BEAT_SECONDS = 90.0


def queue_pr(n: int, source: str | None = None) -> dict:
    """Mark PR `n` queued for sandbox verification. Raises ValueError with the
    operator-readable reason when the pre-check refuses. `source` stamps who
    queued it ("auto" for the idle hunter; the operator path passes None)."""
    rec = data.store().load_pr(n)
    if rec is None:
        raise ValueError(f"PR #{n} not in store")
    if rec.state != "open":
        raise ValueError(f"PR #{n} is {rec.state}, not open")
    if rec.threat_verdict == "malicious":
        raise ValueError(f"PR #{n} has a malicious threat verdict — "
                         f"flagged code never runs in the sandbox")
    status = (rec.verify_request or {}).get("status")
    if status in ("queued", "running", "waiting-for-base"):
        raise ValueError(f"PR #{n} already has a {status} verification request")
    rec.record_verify_request("queued", queued_at=_now(), source=source)
    data.refresh()
    return {"pr": n, "status": "queued"}


def dequeue_pr(n: int) -> dict:
    """Cancel PR `n`'s verification request. Only a request the runner has not
    started — `queued` or `waiting-for-base` — can be cancelled; a running
    sandbox is not interrupted from the cockpit."""
    rec = data.store().load_pr(n)
    if rec is None:
        raise ValueError(f"PR #{n} not in store")
    req = rec.verify_request or {}
    if req.get("status") not in ("queued", "waiting-for-base"):
        raise ValueError(f"PR #{n} is not queued "
                         f"(status: {req.get('status') or 'none'})")
    rec.record_verify_request("cancelled", queued_at=req.get("queued_at"),
                              finished_at=_now())
    data.refresh()
    return {"pr": n, "status": "cancelled"}


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
    """The verify_worker registry's per-host heartbeat records, freshest
    first."""
    hosts = reg.get("hosts") or {}
    return sorted(hosts.values(),
                  key=lambda r: str(r.get("last_beat") or ""), reverse=True)


def runner_status() -> dict:
    """Whether a verification worker is alive against this store: `configured`
    says this backend runs one, `online` says some machine's worker beat within
    STALE_BEAT_SECONDS (the queue is shared, so the runner may be elsewhere).
    `host`/`current_pr`/`last_beat` describe the freshest worker; `hosts`
    carries every known worker's liveness."""
    from review_cockpit.backend import verify_worker
    records = worker_records(data.store().load_verify_worker())
    hosts = [{"host": r.get("host"), "online": _beat_online(r.get("last_beat")),
              "last_beat": r.get("last_beat"), "current_pr": r.get("current_pr"),
              "autohunt": bool(r.get("autohunt"))} for r in records]
    fresh: dict = hosts[0] if hosts else {}
    return {"configured": verify_worker.enabled(),
            "online": any(h["online"] for h in hosts),
            "host": fresh.get("host"), "current_pr": fresh.get("current_pr"),
            "last_beat": fresh.get("last_beat"), "hosts": hosts}
