"""One terse answer to "what is the system doing right now", for the header
status label. Merges both lanes' in-flight queue entries with the shared worker
heartbeat registries, so any app shows work running on any machine — the queue
and the heartbeats live in the shared store, not on the machine doing the work.
"""
from __future__ import annotations

from typing import TypedDict

from prospector_app.backend import data, fix_queue, jobs, verify_queue


class ActiveEntry(TypedDict):
    lane: str
    pr: int
    title: str | None
    action: str | None
    step: str | None
    host: str | None
    started_at: str | None
    source: str | None
    worker_online: bool


class WorkerEntry(TypedDict):
    lane: str
    host: str | None
    online: bool
    last_beat: str | None
    current_pr: int | None
    autohunt: bool


# Fix statuses where a worker is actively holding the request; everything else
# in flight is waiting on a worker tick or an operator.
_FIX_ACTIVE = ("running", "pushing")


def _workers(lane: str, reg: dict, online_check) -> list[WorkerEntry]:
    records = (fix_queue if lane == "fix" else verify_queue).worker_records(reg)
    return [{"lane": lane, "host": r.get("host"),
             "online": online_check(r.get("last_beat")),
             "last_beat": r.get("last_beat"), "current_pr": r.get("current_pr"),
             "autohunt": bool(r.get("autohunt"))} for r in records]


def now() -> dict:
    """Active runs, queued/parked counts, every known worker's liveness, and
    this backend's running jobs. `worker_online` on an active entry asks whether
    the claiming host's worker is still beating — a claimed run whose worker
    went quiet is stuck, not slow."""
    st = data.store()
    verify_workers = _workers("verify", st.load_verify_worker(),
                              verify_queue.beat_online)
    fix_workers = _workers("fix", st.load_fix_worker(), fix_queue.beat_online)
    online = {(w["lane"], w["host"]): w["online"]
              for w in verify_workers + fix_workers}

    active: list[ActiveEntry] = []
    queued = {"verify": 0, "fix": 0}
    awaiting_review = 0
    for e in verify_queue.queue_entries():
        if e["status"] == "running":
            active.append({"lane": "verify", "pr": e["pr"], "title": e["title"],
                           "action": None, "step": e["step"], "host": e["host"],
                           "started_at": e["started_at"], "source": e["source"],
                           "worker_online": online.get(("verify", e["host"]), False)})
        else:  # queued or waiting-for-base — both are work a worker will pick up
            queued["verify"] += 1
    for f in fix_queue.queue_entries():
        if f["status"] in _FIX_ACTIVE:
            active.append({"lane": "fix", "pr": f["pr"], "title": f["title"],
                           "action": f["action"], "step": f["step"],
                           "host": f["host"], "started_at": f["started_at"],
                           "source": f["source"],
                           "worker_online": online.get(("fix", f["host"]), False)})
        elif f["status"] == "awaiting-review":
            awaiting_review += 1
        else:  # queued or approved — waiting on a worker tick
            queued["fix"] += 1

    running_jobs = [j for j in jobs.list_jobs() if j["status"] == "running"]
    return {"active": active, "queued": queued,
            "awaiting_review": awaiting_review,
            "workers": verify_workers + fix_workers,
            "jobs": {"running": len(running_jobs),
                     "labels": [j["label"] for j in running_jobs]}}
