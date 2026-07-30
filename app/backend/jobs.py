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
from datetime import datetime
from pathlib import Path

from app.backend import data
from app.backend import subproc

REPO_ROOT = Path(__file__).resolve().parents[2]
# Phases run via `uv run python` from REPO_ROOT (set as cwd in run_job), which
# auto-syncs the single repo-root uv venv — the same invocation used everywhere else.
PIPELINE_PY = ["uv", "run", "python"]

JOB_SPECS: dict[str, dict] = {
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

JOBS: dict[int, dict] = {}
_counter = {"n": 0}


def list_specs() -> list[dict]:
    return [{"kind": k, "label": v["label"], "needs_cluster": v.get("needs_cluster", False),
             "needs_pr": v.get("needs_pr", False), "needs_count": v.get("needs_count", False)}
            for k, v in JOB_SPECS.items()]


def list_jobs() -> list[dict]:
    keys = ("id", "kind", "cluster", "pr", "count", "status", "label", "started", "returncode")
    return [{k: j[k] for k in keys} for j in sorted(JOBS.values(), key=lambda x: x["id"], reverse=True)]


def start_job(kind: str, cluster: int | None = None, pr: int | None = None,
              count: int | None = None) -> dict:
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
    _counter["n"] += 1
    job = {
        "id": _counter["n"], "kind": kind, "cluster": cluster, "pr": pr, "count": count,
        "label": spec["label"], "status": "running", "log": [],
        "started": datetime.now().isoformat(), "returncode": None,
        "_argv": (spec["argv_fn"](target) if spec.get("argv_fn") else spec["argv"]),
        "_wake": asyncio.Event(),
    }
    JOBS[job["id"]] = job
    return job


def _emit(job: dict, line: str) -> None:
    """Append a log line and wake any attached SSE readers. Each waiter holds
    the Event it's awaiting, so replacing it here (rather than clearing it)
    can never race a waiter that already grabbed the old one."""
    job["log"].append(line)
    job["_wake"].set()
    job["_wake"] = asyncio.Event()


async def run_job(job: dict) -> None:
    """Drive `job`'s subprocess to completion. Scheduled once via
    `asyncio.create_task` right after `start_job`, independent of any SSE
    reader — the job runs to completion (updating `log`/`status`/`returncode`
    on `job` throughout) whether or not a client is attached, so navigating
    away from the page that started it neither stops nor orphans it (#683).
    An unattached child would otherwise stall once its stdout pipe fills,
    since nothing would be reading it."""
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


async def attach_job(job: dict):
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
