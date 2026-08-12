"""Control-panel job runner — pipeline-v2 phases.

Runs a FIXED, allowlisted set of pipeline phases and streams output as SSE.
The set is closed — the UI cannot run arbitrary commands; the only free
parameter is a validated integer cluster id.

  - selftest      : trivial echo (proves the streaming path)
  - ingest        : pipeline phase 0 — refresh PR meta + signals, then the chained
                    issue ingest (read-only gh)
  - issue-ingest  : issue pipeline INGEST alone — refresh issues + reconcile
                    closures (read-only gh)
  - issue-analyze : issue pipeline ANALYZE — parallel-batch dispositions for the
                    `count` lowest-id pending issues (store writes only, nothing
                    upstream)
  - issue-find-fixed : detect already-fixed open issues (gh-heavy, pain-ranked waves)
  - analyze-clusters : ANALYZE phase — parallel per-cluster dispositions for the
                        `count` lowest-id pending clusters (gh reads only, store
                        writes; no upstream writes)

CLUSTER / SECURITY phase buttons land with their phases.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict
from weakref import WeakKeyDictionary

from prospector_app.backend import data
from prospector_app.backend import subproc

REPO_ROOT = Path(__file__).resolve().parents[2]
# Phases run via `uv run python` from REPO_ROOT (set as cwd in run_job), which
# auto-syncs the single repo-root uv venv — the same invocation used everywhere else.
PIPELINE_PY = ["uv", "run", "python"]


class JobSpec(TypedDict):
    label: str
    argv: NotRequired[list[str]]
    argv_fn: NotRequired[Callable[[int], list[str]]]
    needs_cluster: NotRequired[bool]
    needs_pr: NotRequired[bool]
    needs_count: NotRequired[bool]
    max_concurrency: NotRequired[int]


JobStatus = Literal["running", "done", "failed"]


class JobView(TypedDict):
    id: int
    kind: str
    cluster: int | None
    pr: int | None
    count: int | None
    label: str
    status: JobStatus
    started: str
    returncode: int | None


class Job(JobView):
    log: list[str]
    _argv: list[str]
    _wake: asyncio.Event


class JobSpecView(TypedDict):
    kind: str
    label: str
    needs_cluster: bool
    needs_pr: bool
    needs_count: bool


JOB_SPECS: dict[str, JobSpec] = {
    "selftest": {
        "label": "Self-test (echo)",
        "argv": [*PIPELINE_PY, "-u", "-c",
                 "import time\nfor i in range(5):\n    print(f'tick {i}', flush=True)\n    time.sleep(0.3)\nprint('done')"],
    },
    "ingest": {
        "label": "Ingest (refresh PRs + signals, then issues · read-only)",
        "argv": [*PIPELINE_PY, "-u", str(REPO_ROOT / "pipeline" / "ingest.py")],
    },
    "issue-ingest": {
        "label": "Issue ingest (refresh issues + reconcile closures · read-only)",
        "argv": [*PIPELINE_PY, "-u", str(REPO_ROOT / "issue_triage" / "issue_ingest.py")],
    },
    "issue-analyze": {
        "label": "Issue analyze (dispositions for pending issues · agentic)",
        "needs_count": True,
        "argv_fn": lambda n: [*PIPELINE_PY, "-u",
                              str(REPO_ROOT / "issue_triage" / "analyze_issues.py"),
                              "--limit", str(n)],
    },
    "issue-find-fixed": {
        "label": "Issue find-fixed (detect already-fixed issues · agentic · gh-heavy)",
        "needs_count": True,
        "argv_fn": lambda n: [*PIPELINE_PY, "-u",
                              str(REPO_ROOT / "issue_triage" / "find_fixed.py"),
                              "--limit", str(n)],
    },
    "alert-ingest": {
        "label": "Alert ingest (refresh security alerts as the bot · read-only)",
        "argv": [*PIPELINE_PY, "-u", str(REPO_ROOT / "alert_triage" / "alert_ingest.py")],
    },
    "alert-find-fixed": {
        "label": "Alert find-fixed (detect already-fixed alerts · agentic · gh-heavy)",
        "needs_count": True,
        "argv_fn": lambda n: [*PIPELINE_PY, "-u",
                              str(REPO_ROOT / "alert_triage" / "find_fixed.py"),
                              "--limit", str(n)],
    },
    "threat-scan": {
        "label": "Threat scan (deterministic · fetches uncached diffs · read-only)",
        "argv": [*PIPELINE_PY, "-u", str(REPO_ROOT / "pipeline" / "threat_scan.py")],
    },
    "threat-scan-pr": {
        "label": "Threat scan (single PR)",
        "needs_pr": True,
        "argv_fn": lambda pr: [*PIPELINE_PY, "-u",
                               str(REPO_ROOT / "pipeline" / "threat_scan.py"),
                               "--only", str(pr)],
    },
    "triage-cluster": {
        "label": "Triage cluster (refresh facts + classify)",
        "needs_cluster": True,
        "argv_fn": lambda cid: [*PIPELINE_PY, "-u",
                                str(REPO_ROOT / "pipeline" / "triage_cluster.py"),
                                "--cluster", str(cid)],
    },
    "analyze-clusters": {
        "label": "Analyze clusters (dispositions for pending clusters · agentic)",
        "needs_count": True,
        "argv_fn": lambda n: [*PIPELINE_PY, "-u",
                              str(REPO_ROOT / "pipeline" / "analyze_clusters.py"),
                              "--limit", str(n)],
    },
    "security-review": {
        "label": "Security review (single PR)",
        "needs_pr": True,
        "max_concurrency": 2,
        "argv_fn": lambda pr: [*PIPELINE_PY, "-u",
                               str(REPO_ROOT / "pipeline" / "security_review.py"),
                               "--pr", str(pr)],
    },
    "verify-pr": {
        "label": "Sandbox verification (single PR · needs the Docker sandbox)",
        "needs_pr": True,
        "argv_fn": lambda pr: [*PIPELINE_PY, "-u",
                               str(REPO_ROOT / "pipeline" / "verify_pr.py"),
                               "--pr", str(pr)],
    },
}

JOBS: dict[int, Job] = {}
_counter = {"n": 0}
_TASKS: set[asyncio.Task[None]] = set()
# Async primitives belong to their event loop. The app uses one loop while
# TestClient and unit tests may create additional loops in the same process.
_LIMITERS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]
] = WeakKeyDictionary()


def list_specs() -> list[JobSpecView]:
    return [{"kind": k, "label": v["label"], "needs_cluster": v.get("needs_cluster", False),
             "needs_pr": v.get("needs_pr", False), "needs_count": v.get("needs_count", False)}
            for k, v in JOB_SPECS.items()]


def list_jobs() -> list[JobView]:
    return [{"id": job["id"], "kind": job["kind"], "cluster": job["cluster"],
             "pr": job["pr"], "count": job["count"], "status": job["status"],
             "label": job["label"], "started": job["started"],
             "returncode": job["returncode"]}
            for job in sorted(JOBS.values(), key=lambda item: item["id"], reverse=True)]


def start_job(kind: str, cluster: int | None = None, pr: int | None = None,
              count: int | None = None) -> Job:
    spec = JOB_SPECS.get(kind)
    if not spec:
        raise ValueError(f"unknown job kind: {kind}")
    if spec.get("needs_cluster") and cluster is None:
        raise ValueError("this job needs a cluster id")
    if spec.get("needs_pr") and pr is None:
        raise ValueError("this job needs a PR number")
    if spec.get("needs_count") and (count is None or count < 1):
        raise ValueError("this job needs a positive count")
    # one running job per (kind, target) — re-runs of the same cluster/PR collide
    target = pr if spec.get("needs_pr") else (count if spec.get("needs_count") else cluster)
    if (spec.get("needs_cluster") or spec.get("needs_pr")) and any(
            j["kind"] == kind and (j["pr"] if spec.get("needs_pr") else j["cluster"]) == target
            and j["status"] == "running"
            for j in JOBS.values()):
        raise ValueError(f"a {kind} job for {'PR' if spec.get('needs_pr') else 'cluster'} "
                         f"{target} is already running")
    argv_fn = spec.get("argv_fn")
    if argv_fn is not None:
        assert target is not None
        argv = argv_fn(target)
    else:
        argv = spec.get("argv")
        if argv is None:
            raise ValueError(f"job kind {kind} has no command")
    _counter["n"] += 1
    job: Job = {
        "id": _counter["n"], "kind": kind, "cluster": cluster, "pr": pr, "count": count,
        "label": spec["label"], "status": "running", "log": [],
        "started": datetime.now().isoformat(), "returncode": None,
        "_argv": argv, "_wake": asyncio.Event(),
    }
    JOBS[job["id"]] = job
    return job


def _emit(job: Job, line: str) -> None:
    """Append a log line and wake attached SSE readers. Each waiter retains its
    current Event while the job installs the Event for the next log line."""
    job["log"].append(line)
    job["_wake"].set()
    job["_wake"] = asyncio.Event()


async def run_job(job: Job) -> None:
    """Drive `job`'s subprocess to completion. Scheduled once via
    `asyncio.create_task` right after `start_job`, independent of any SSE
    reader — the job runs to completion (updating `log`/`status`/`returncode`
    on `job` throughout) whether or not a client is attached, so navigating
    away from the page that started it neither stops nor orphans it (#683).
    The runner continuously drains the child's stdout pipe."""
    argv = job["_argv"]
    _emit(job, f"$ {' '.join(str(a)[:60] for a in argv[:4])}…")
    try:
        proc = await subproc.spawn(argv, cwd=REPO_ROOT, stderr=asyncio.subprocess.STDOUT)
        assert proc.stdout is not None
        async for raw in proc.stdout:
            text = raw.decode("utf-8", "replace").rstrip("\n")
            if text.strip():
                _emit(job, text)
        await proc.wait()
        job["returncode"] = proc.returncode
        job["status"] = "done" if proc.returncode == 0 else "failed"
    except Exception as e:  # the child never started, or died unexpectedly
        job["returncode"] = -1
        job["status"] = "failed"
        _emit(job, f"! job crashed: {e}")
    finally:
        data.refresh()  # store may have changed
        job["_wake"].set()


def _job_limiter(kind: str) -> asyncio.Semaphore | None:
    limit = JOB_SPECS[kind].get("max_concurrency")
    if limit is None:
        return None
    loop = asyncio.get_running_loop()
    by_kind = _LIMITERS.setdefault(loop, {})
    return by_kind.setdefault(kind, asyncio.Semaphore(limit))


async def _run_scheduled_job(job: Job) -> None:
    """Run one job under its kind's process-concurrency limit, when configured."""
    limiter = _job_limiter(job["kind"])
    if limiter is None:
        await run_job(job)
        return
    async with limiter:
        await run_job(job)


def schedule_job(job: Job) -> None:
    """Schedule one job and retain its task through completion."""
    task = asyncio.create_task(_run_scheduled_job(job))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def attach_job(job: Job) -> AsyncIterator[dict[str, str]]:
    """Stream `job`'s log as SSE — replaying everything captured so far, then
    following live, then a final `done` event. Safe to call more than once
    per job (a fresh attach after navigating back sees the full history
    immediately) and safe to abandon at any point: closing this generator
    only drops this reader, `run_job` above keeps the process going."""
    i = 0
    while True:
        while i < len(job["log"]):
            yield {"event": "log", "data": job["log"][i]}
            i += 1
        if job["status"] != "running":
            break
        await job["_wake"].wait()
    yield {"event": "done", "data": json.dumps({"returncode": job["returncode"], "status": job["status"]})}
