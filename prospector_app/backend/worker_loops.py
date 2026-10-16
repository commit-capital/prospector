"""This machine's lanes and the last pass of each periodic loop, in its own
`worker_loops:<host>` row, so any app can read which lanes a machine runs and
when its loops run next. Writing is best-effort: a failed write is printed and
the loop carries on.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone

from pipeline import settings
from prospector_app.backend import data, schema_gate

LABELS: dict[str, str] = {
    "threat-refresh": "Threat scan of new heads",
    "pr-watch": "PR watch",
    "stale-refresh": "Stale merge-candidate refresh",
    "rereview": "Re-review requests",
    "responses": "Author responses sweep",
    "cluster": "Clustering pass",
    "display": "PR summary cards",
    "issue-followup": "Issue follow-up",
    "docker-disk": "Docker disk check",
}

_lock = threading.Lock()
_passes: dict[str, dict] = {}


def lanes_on() -> dict[str, bool]:
    # The worker modules import this one to stamp their loops.
    from prospector_app.backend import cluster_refresh, fix_worker, issue_fix_worker, verify_worker
    hunting = verify_worker.enabled() and verify_worker.enabled_autohunt()
    return {"security": hunting, "verify": verify_worker.enabled(),
            "design": hunting and settings.design_review_mode() != "off",
            "autofix": fix_worker.enabled(), "issue-fix": issue_fix_worker.enabled(),
            "cluster": cluster_refresh.enabled()}


def _write(now: datetime) -> None:
    if schema_gate.paused("worker-loops"):
        return
    try:
        data.store().save_worker_loops({"host": settings.worker_id(), "at": now.isoformat(),
                                        "lanes": lanes_on(), "loops": dict(_passes)})
    except Exception:
        traceback.print_exc()


def ran(loop: str, every_seconds: int, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    with _lock:
        _passes[loop] = {"last_at": now.isoformat(), "every_seconds": every_seconds}
        _write(now)


def publish(now: datetime | None = None) -> None:
    with _lock:
        _write(now or datetime.now(timezone.utc))
