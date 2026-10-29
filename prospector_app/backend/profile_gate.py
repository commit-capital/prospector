"""Whether the live workers' repository profiles disagree. Each machine reads
its own profile file, and the profile decides safety verdicts (risk tiers,
fixable gates, trusted authors), so while two live workers' profiles differ
the same request could be withheld on one machine and pushed on another.
Every work loop on every machine then sits out its pass, heartbeats keep
recording each machine's profile stamp, and the health strip names how the
machines differ.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from prospector_app.backend import data, schema_gate, verify_queue

if TYPE_CHECKING:
    from pipeline.store import WorkerRegistries

# The registries are one read, and every loop asks before each pass.
_TTL_SECONDS = 60.0

_lock = threading.Lock()
_cache: tuple[float, dict[str, dict]] | None = None
_announced: set[str] = set()


def drift(regs: WorkerRegistries) -> dict[str, dict]:
    """Each live machine's profile stamp, from its newest heartbeat, when they
    do not all match; empty when they do. A machine whose newest beat is
    offline or carries no stamp is not judged."""
    stamps = {host: rec["profile"] for host, rec in schema_gate.newest_beats(regs).items()
              if verify_queue.beat_online(rec.get("last_beat"))
              and isinstance(rec.get("profile"), dict)}
    return stamps if len({s.get("fingerprint") for s in stamps.values()}) > 1 else {}


def _current() -> dict[str, dict]:
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache and now - _cache[0] < _TTL_SECONDS:
            return _cache[1]
    d = drift(data.store().load_worker_registries())
    with _lock:
        _cache = (now, d)
    return d


def paused(lane: str) -> bool:
    """Whether `lane` sits this pass out because the live workers' profiles
    differ, saying so once each time the drift starts. A failed read pauses
    nothing: the loop's own error handling reports it."""
    try:
        d = _current()
    except Exception:
        return False
    if not d:
        _announced.discard(lane)
        return False
    if lane not in _announced:
        _announced.add(lane)
        hosts = ", ".join(f"{h} {s.get('fingerprint')}" for h, s in sorted(d.items()))
        print(f"[{lane}] paused: the live workers' profiles differ ({hosts}) — make "
              f"their TRIAGE_PROFILE files match and restart", flush=True)
    return True
