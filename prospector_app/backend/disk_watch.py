"""Keeping each sandbox machine's Docker disk reading current, so the health
strip warns while the disk is filling and before a full one fails every
sandbox run. A machine that runs a verify, autofix or issue-fix worker reads
`verify_driver.docker_disk` every INTERVAL_SECONDS onto its worker health
record; `system_health` judges it.
"""
from __future__ import annotations

import threading
import traceback

from pipeline import settings, verify_driver, worker_health
from prospector_app.backend import data, schema_gate, worker_loops

INTERVAL_SECONDS = 5 * 60

_thread: threading.Thread | None = None
_stop = threading.Event()


def enabled() -> bool:
    return (settings.verify_worker_enabled() or settings.fix_worker_enabled()
            or settings.issue_fix_worker_enabled())


def check_once() -> None:
    try:
        reading: dict[str, int] | str = verify_driver.docker_disk()
    except Exception as e:  # a probe that cannot answer is recorded, not raised
        reading = f"{type(e).__name__}: {e}"
    min_free = settings.docker_min_free_gb() * 10**9
    worker_health.update(data.store(), settings.worker_id(), lambda r: worker_health.record_disk(
        r, reading, min_free_bytes=min_free))


def _loop() -> None:
    while not _stop.is_set():
        try:
            if not schema_gate.paused("docker-disk"):
                check_once()
        except Exception:
            traceback.print_exc()
        worker_loops.ran("docker-disk", INTERVAL_SECONDS)
        _stop.wait(INTERVAL_SECONDS)


def start() -> bool:
    """Start the cadence on a sandbox machine (`enabled`). Idempotent."""
    global _thread
    if not enabled():
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="docker-disk")
    _thread.start()
    return True
