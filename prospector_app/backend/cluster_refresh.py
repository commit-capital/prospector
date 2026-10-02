"""The clustering lane: CLUSTER and ANALYZE run unattended on a worker.

On a machine with TRIAGE_CLUSTER_WORKER=1, every REFRESH_SECONDS this runs one
incremental pass (pipeline/cluster_pass.py) in a child process under
PROSPECTOR_UNATTENDED, so each agent call the pass makes is gated by the
account's AI capacity policy and booked as the `cluster` lane's spend. A pass
takes at most PASS_PRS PRs and PASS_CLUSTERS clusters, and one UTC day's passes
together at most settings.cluster_daily_prs() and
settings.cluster_daily_clusters(), counted from the runs-ledger rows the passes
stamp with TRIGGER. A pass starts only when the store shows clustering work,
the lane is open, the capacity gate is open, and this machine holds the LEASE,
so one machine runs the lane at a time.

The ending is booked on the lane's health: a finished pass is a success, an
agent outage trips every agent lane, a spent usage limit books nothing, and any
other exit is a machine failure.
"""
from __future__ import annotations

import os
import subprocess
import threading
import traceback
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pipeline import capacity, cluster_pass, freshness, profile, settings, storekit
from prospector_app.backend import data, lane_health
from prospector_app.backend.jobs import PIPELINE_PY, REPO_ROOT

if TYPE_CHECKING:
    from pipeline.model import Pr

LANE = "cluster"
TRIGGER = "worker"
REFRESH_SECONDS = 60 * 60
PASS_PRS = 50
PASS_CLUSTERS = 10
LEASE = "cluster-lane"
# Longer than any pass, so a lease outlives its holder's pass only when the
# holder died mid-pass.
LEASE_SECONDS = 4 * 3600
SHUTDOWN_TIMEOUT = 10.0

_thread: threading.Thread | None = None
_stop = threading.Event()


def spent_today(runs: Iterable[storekit.RunRecord], today: str) -> tuple[int, int]:
    """(PRs summarized, clusters analyzed) by the lane's passes on UTC day
    `today` (YYYY-MM-DD)."""
    prs = clusters = 0
    for rec in runs:
        if not isinstance(rec, storekit.PhaseRun) or rec.raw.get("trigger") != TRIGGER:
            continue
        if not str(rec.finished or rec.started or "").startswith(today):
            continue
        attempted = (rec.raw.get("stats") or {}).get("attempted")
        if not isinstance(attempted, int):
            continue
        if rec.phase == "cluster:summaries":
            prs += attempted
        elif rec.phase == "analyze:commit":
            clusters += attempted
    return prs, clusters


def has_work(prs: dict[int, Pr]) -> bool:
    """Whether an open PR lacks a current summary (a configured automation
    author's aside), is summarized but in no cluster and not stamped
    standalone, or belongs to a cluster without a current analysis of it."""
    bots = profile.active().automation_bots
    for pr in prs.values():
        if pr.state != "open":
            continue
        if not freshness.is_current(pr, "summary"):
            if pr.author not in bots:
                return True
            continue
        if not pr.section("cluster"):
            return True
        if pr.cluster_ids and not freshness.is_current(pr, "analysis"):
            return True
    return False


def _spawn(limit: int, analyze: int) -> tuple[int, str]:
    """Run one pass in a child process; its exit code and last output line."""
    argv = [*PIPELINE_PY, "-u", str(REPO_ROOT / "pipeline" / "cluster_pass.py"),
            "--limit", str(limit), "--analyze", str(analyze), "--trigger", TRIGGER]
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, capacity.UNATTENDED_ENV: LANE})
    assert proc.stdout is not None
    last = ""
    for line in proc.stdout:
        print(f"[cluster] {line}", end="", flush=True)
        if line.strip():
            last = line.strip()
    return proc.wait(), last


def book(rc: int, last: str) -> None:
    if rc == 0:
        lane_health.note_success(LANE)
    elif rc == cluster_pass.EXIT_AGENT_UNAVAILABLE:
        lane_health.trip_agent_lanes(last or "the agent CLI could not run")
    elif rc != cluster_pass.EXIT_LIMIT:
        lane_health.note_failure(LANE, kind="cluster-pass",
                                 reason=f"cluster pass exited {rc}: {last}"[:300])


def pass_once() -> int | None:
    """Run one pass when one is due; its exit code, or None when none ran."""
    today = datetime.now(timezone.utc).date().isoformat()
    used_prs, used_clusters = spent_today(data.runs(since=today), today)
    prs_left = max(0, settings.cluster_daily_prs() - used_prs)
    clusters_left = max(0, settings.cluster_daily_clusters() - used_clusters)
    if not (prs_left or clusters_left):
        return None
    if not has_work(data.prs()):
        return None
    if not lane_health.open_or_retest(LANE) or not lane_health.capacity_open(LANE):
        return None
    host = settings.worker_id()
    store = data.store()
    if not store.claim_lease(LEASE, host=host, seconds=LEASE_SECONDS):
        return None
    try:
        rc, last = _spawn(min(PASS_PRS, prs_left), min(PASS_CLUSTERS, clusters_left))
    finally:
        store.release_lease(LEASE, host=host)
        data.refresh()
    book(rc, last)
    return rc


def _loop() -> None:
    while not _stop.is_set():
        try:
            pass_once()
        except Exception:
            traceback.print_exc()
        _stop.wait(REFRESH_SECONDS)


def enabled() -> bool:
    return settings.cluster_worker_enabled()


def running() -> bool:
    return _thread is not None and _thread.is_alive()


def startup() -> bool:
    """Start the lane on a clustering worker. Idempotent; returns whether it
    runs."""
    global _thread
    if not enabled():
        return False
    if running():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="cluster-refresh")
    _thread.start()
    return True


def shutdown(timeout: float = SHUTDOWN_TIMEOUT) -> bool:
    """Signal the lane to stop and wait for it; a pass in flight finishes."""
    _stop.set()
    if _thread is not None:
        _thread.join(timeout)
    return not running()
