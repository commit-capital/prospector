"""Telling a human that a worker has stopped working properly.

A tripped lane and a worker that stopped beating are the two conditions the
system cannot fix on its own. Both reach the operator through the app: the
health strip atop every page and the Control tab's banner read the worker
health records and the heartbeats live, so an alert clears the moment its
condition does. A trip also appends a `worker:trip` entry to the runs ledger.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone

from pipeline import settings, worker_health
from pipeline.storekit import now as _now
from prospector_app.backend import data, verify_queue


def escalate_trip(lanes: list[str]) -> None:
    """Record this worker's fresh trip of `lanes` (one cause, one or more
    lanes) in the ledger."""
    if not lanes:
        return
    host = settings.worker_id()
    st = data.store()
    tripped = worker_health.lane(worker_health.load(st, host), lanes[0]).get("tripped") or {}
    kind = str(tripped.get("kind") or "unknown")
    reason = str(tripped.get("reason") or "")
    try:
        st.append_run({"phase": "worker:trip", "started": _now(), "finished": _now(),
                       "stats": {"host": host, "lanes": list(lanes), "kind": kind,
                                 "reason": reason[:600]}})
    except Exception:
        traceback.print_exc()
    print(f"[escalation] {', '.join(lanes)} tripped on {host}: {reason[:200]}", flush=True)


def offline_workers(now: datetime | None = None) -> list[dict]:
    """Every worker whose last heartbeat, in either lane's registry, is older
    than OFFLINE_AFTER_SECONDS: `{host, lane, last_beat, age_seconds}`."""
    now = now or datetime.now(timezone.utc)
    st = data.store()
    out: list[dict] = []
    for lane, reg in (("verify", st.load_verify_worker()),
                      ("fix", st.load_fix_worker())):
        for host, r in (reg.get("hosts") or {}).items():
            last = r.get("last_beat")
            if verify_queue.beat_online(last):
                continue
            try:
                age = (now - datetime.fromisoformat(str(last))).total_seconds()
            except ValueError:
                continue
            if age >= worker_health.OFFLINE_AFTER_SECONDS:
                out.append({"host": host, "lane": lane, "last_beat": last,
                            "age_seconds": age})
    return out


def health_status() -> dict:
    """Every worker's lane health for the app: `{hosts: [{host, lanes}]}`,
    tripped lanes first."""
    hosts = data.store().load_worker_health().get("hosts") or {}
    out = []
    for host, rec in sorted(hosts.items()):
        lanes = {name: {k: entry.get(k) for k in
                        ("consecutive_failures", "tripped", "retest",
                         "recent", "last_success_at")}
                 for name, entry in (rec.get("lanes") or {}).items()}
        out.append({"host": host, "lanes": lanes,
                    "tripped": sorted(n for n, e in lanes.items() if e.get("tripped"))})
    out.sort(key=lambda h: (not h["tripped"], h["host"]))
    return {"hosts": out, "any_tripped": any(h["tripped"] for h in out)}


def resume(host: str, lane: str) -> dict:
    """An operator's Resume: reopen the lane. Raises ValueError on an unknown
    lane or a worker with no health record."""
    if lane not in worker_health.LANES:
        raise ValueError(f"unknown lane {lane!r}")
    if host not in (data.store().load_worker_health().get("hosts") or {}):
        raise ValueError(f"no worker named {host!r} has recorded any health")
    rec = worker_health.update(data.store(), host, lambda r: worker_health.reopen(
        r, lane, by="resumed by the operator"))
    try:
        data.store().append_run({"phase": "worker:resume", "started": _now(),
                                 "finished": _now(),
                                 "stats": {"host": host, "lane": lane}})
    except Exception:
        traceback.print_exc()
    return json.loads(json.dumps(rec))
