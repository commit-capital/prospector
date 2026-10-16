"""Whether this machine's code is behind the shared store's schema. Code that
is behind can read the store but not write it (`storekit.assert_writable`), so
every worker loop sits out its pass while `paused` says so, and the health
strip asks the operator to update this machine.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from pipeline import schema, storekit
from prospector_app.backend import data, fix_queue, verify_queue

if TYPE_CHECKING:
    from pipeline.store import WorkerRegistries

# The stamp is one registry row, and every loop asks before each pass.
_TTL_SECONDS = 60.0

_lock = threading.Lock()
_cache: tuple[float, int] | None = None
_announced: set[str] = set()


def store_version() -> int:
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache and now - _cache[0] < _TTL_SECONDS:
            return _cache[1]
    v = storekit.store_schema_version(data.store().engine)
    with _lock:
        _cache = (now, v)
    return v


def behind() -> bool:
    return store_version() > schema.STORE_SCHEMA_VERSION


def paused(lane: str) -> bool:
    """Whether `lane` sits this pass out because the code is behind the store,
    saying so once per lane. A failed read pauses nothing: the loop's own
    error handling reports it."""
    try:
        if not behind():
            return False
    except Exception:
        return False
    if lane not in _announced:
        _announced.add(lane)
        print(f"[{lane}] paused: the store is on schema v{store_version()} and this "
              f"Prospector is v{schema.STORE_SCHEMA_VERSION} — pull main and restart",
              flush=True)
    return True


def outdated_hosts(regs: WorkerRegistries, store_v: int, me: str) -> dict[str, int]:
    """Each machine whose code is behind `store_v`, by the schema version its
    last heartbeat recorded, and this machine by its own code."""
    out: dict[str, int] = {}
    beats = [*verify_queue.worker_records(regs["verify_worker"]),
             *fix_queue.worker_records(regs["fix_worker"]),
             *(regs["issue_fix_worker"].get("hosts") or {}).values()]
    for rec in beats:
        v, host = rec.get("schema"), rec.get("host")
        if isinstance(v, int) and host and v < store_v:
            out[str(host)] = min(v, out.get(str(host), v))
    if schema.STORE_SCHEMA_VERSION < store_v:
        out[me] = schema.STORE_SCHEMA_VERSION
    else:
        out.pop(me, None)
    return out
