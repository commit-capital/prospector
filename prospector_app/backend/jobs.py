"""Control-panel job runner — pipeline-v2 phases.

Runs a FIXED, allowlisted set of pipeline phases and streams output as SSE.
The set is closed — the UI cannot run arbitrary commands; the only free
parameter is a validated integer cluster id, PR number, or count.

  - selftest      : trivial echo (proves the streaming path)
  - ingest        : pipeline phase 0 — refresh PR meta + signals, then the chained
                    issue ingest (read-only gh)
  - issue-ingest  : issue pipeline INGEST alone — refresh issues + reconcile
                    closures (read-only gh)
  - issue-analyze : issue pipeline ANALYZE — parallel-batch dispositions for the
                    `count` lowest-id pending issues (store writes only, nothing
                    upstream)
  - issue-find-fixed : detect already-fixed open issues (gh-heavy, pain-ranked waves)
  - security-sweep : alert ingest → alert find-fixed → advisory ingest → advisory
                     find-fixed, one process (bot reads, store writes, agents)
  - analyze-clusters : ANALYZE phase — parallel per-cluster dispositions for the
                        `count` lowest-id pending clusters (gh reads only, store
                        writes; no upstream writes)

Every job is recorded under JOBS_DIR as `<id>.json` (its record), `<id>.log`
(the child's combined output, which the child writes directly) and `<id>.exit`
(the exit code, written by the `sh` wrapper the child runs under). The child
runs in its own session, so a backend restart — a dev-server reload, a crash —
neither kills it nor loses it: `restore` reloads the records and follows each
job still running from its log file to its exit code.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NotRequired, TypedDict
from weakref import WeakKeyDictionary

from pipeline import progress
from pipeline import storekit
from prospector_app.backend import data

REPO_ROOT = Path(__file__).resolve().parents[2]
# Phases run via `uv run python` from REPO_ROOT (set as cwd in run_job), which
# auto-syncs the single repo-root uv venv — the same invocation used everywhere else.
PIPELINE_PY = ["uv", "run", "python"]

JOBS_DIR = REPO_ROOT / "prospector_app" / "cache" / "jobs"
# Finished jobs kept on disk; queued and running ones are never pruned.
KEEP_FINISHED = 50
# How often a running job's log file is read for new output, and the most of
# it one read takes, so a burst of output never holds the event loop for long.
POLL_SECONDS = 0.25
CATCH_UP_BYTES = 1024 * 1024
# How long a stopped job's processes get to exit on SIGTERM before SIGKILL.
STOP_GRACE_SECONDS = 10.0
# Runs the job's argv (after the exit-file path) and records its exit code.
_EXIT_WRAPPER = ('exit_file=$1; shift; "$@"; rc=$?; '
                 'printf "%s\\n" "$rc" > "$exit_file.tmp" && mv "$exit_file.tmp" "$exit_file"; '
                 'exit "$rc"')


class JobSpec(TypedDict):
    label: str
    # One sentence on what the job does, for the row under its Run button.
    detail: str
    # Whether the job runs headless agents (costs tokens); False = deterministic.
    agentic: bool
    # Where the job's runs land: a ledger name ("pr" | "issue" | "alert") plus
    # the phase names its runs are stamped with — how the UI shows each job's
    # last run and typical duration. Absent for jobs that leave no ledger row.
    ledger: NotRequired[tuple[str, tuple[str, ...]]]
    argv: NotRequired[list[str]]
    argv_fn: NotRequired[Callable[[int], list[str]]]
    needs_cluster: NotRequired[bool]
    needs_pr: NotRequired[bool]
    needs_count: NotRequired[bool]
    # For a needs_count job: the count the UI offers first, and what it counts.
    count_default: NotRequired[int]
    count_noun: NotRequired[str]
    max_concurrency: NotRequired[int]


JobStatus = Literal["queued", "running", "done", "failed"]


class JobView(TypedDict):
    id: int
    kind: str
    cluster: int | None
    pr: int | None
    count: int | None
    label: str
    status: JobStatus
    started: str
    finished: str | None
    returncode: int | None


class Job(JobView):
    log: list[str]
    pid: int | None
    stop_requested: bool
    _argv: list[str]
    _wake: asyncio.Event
    # Bytes of the log file already split into `log`.
    _log_read: int


class JobSpecView(TypedDict):
    kind: str
    label: str
    detail: str
    agentic: bool
    needs_cluster: bool
    needs_pr: bool
    needs_count: bool
    count_default: int | None
    count_noun: str | None


JOB_SPECS: dict[str, JobSpec] = {
    "ingest": {
        "label": "Ingest",
        "detail": "refresh PR metadata + signals from GitHub, then the chained issue ingest. Cheap and safe to re-run. Read-only.",
        "agentic": False,
        "ledger": ("pr", ("ingest",)),
        "argv": [*PIPELINE_PY, "-u", str(REPO_ROOT / "pipeline" / "ingest.py")],
    },
    "threat-scan": {
        "label": "Threat scan",
        "detail": "deterministic attack-pattern scan over each PR's whole diff + author blocklist check. Fetches any uncached diffs from GitHub first (read-only), so coverage doesn't wait on a Clustering run, and reads the whole diff of any PR whose cached copy is capped.",
        "agentic": False,
        "ledger": ("pr", ("threat-scan",)),
        "argv": [*PIPELINE_PY, "-u", str(REPO_ROOT / "pipeline" / "threat_scan.py")],
    },
    "analyze-clusters": {
        "label": "Analyze clusters",
        "detail": "dispositions for the lowest-id pending clusters, in parallel. gh reads and store writes only, nothing upstream.",
        "agentic": True,
        "ledger": ("pr", ("analyze:commit",)),
        "needs_count": True,
        "count_default": 20,
        "count_noun": "clusters",
        "argv_fn": lambda n: [*PIPELINE_PY, "-u",
                              str(REPO_ROOT / "pipeline" / "analyze_clusters.py"),
                              "--limit", str(n)],
    },
    "issue-ingest": {
        "label": "Issue ingest",
        "detail": "refresh issues + reconcile closures. Read-only.",
        "agentic": False,
        "ledger": ("issue", ("ingest",)),
        "argv": [*PIPELINE_PY, "-u", str(REPO_ROOT / "issue_triage" / "issue_ingest.py")],
    },
    "issue-analyze": {
        "label": "Issue analyze",
        "detail": "dispositions for the lowest-id pending issues, in parallel batches. Store writes only, no fix scan.",
        "agentic": True,
        "ledger": ("issue", ("analyze",)),
        "needs_count": True,
        "count_default": 200,
        "count_noun": "issues",
        "argv_fn": lambda n: [*PIPELINE_PY, "-u",
                              str(REPO_ROOT / "issue_triage" / "analyze_issues.py"),
                              "--limit", str(n)],
    },
    "issue-find-fixed": {
        "label": "Issue find-fixed",
        "detail": "detect already-fixed open issues. gh-heavy, pain-ranked waves.",
        "agentic": True,
        "ledger": ("issue", ("find-fixed",)),
        "needs_count": True,
        "count_default": 12,
        "count_noun": "issues",
        "argv_fn": lambda n: [*PIPELINE_PY, "-u",
                              str(REPO_ROOT / "issue_triage" / "find_fixed.py"),
                              "--limit", str(n)],
    },
    "security-sweep": {
        "label": "Security sweep",
        "detail": "alerts + advisories: ingest as the bot, then find-fixed. gh-heavy.",
        "agentic": True,
        "ledger": ("alert", ("alert-ingest", "alert-find-fixed",
                             "advisory-ingest", "advisory-find-fixed")),
        "needs_count": True,
        "count_default": 12,
        "count_noun": "records",
        "argv_fn": lambda n: [*PIPELINE_PY, "-u",
                              str(REPO_ROOT / "alert_triage" / "security_sweep.py"),
                              "--limit", str(n)],
    },
    "triage-cluster": {
        "label": "Triage cluster",
        "detail": "refresh one cluster's PRs from GitHub, then classify it.",
        "agentic": True,
        "needs_cluster": True,
        "argv_fn": lambda cid: [*PIPELINE_PY, "-u",
                                str(REPO_ROOT / "pipeline" / "triage_cluster.py"),
                                "--cluster", str(cid)],
    },
    "security-review": {
        "label": "Security review (single PR)",
        "detail": "3-lens adversarial review + refuting verifier on one PR.",
        "agentic": True,
        "ledger": ("pr", ("security:review-one",)),
        "needs_pr": True,
        "max_concurrency": 2,
        "argv_fn": lambda pr: [*PIPELINE_PY, "-u",
                               str(REPO_ROOT / "pipeline" / "security_review.py"),
                               "--pr", str(pr)],
    },
    "verify-pr": {
        "label": "Sandbox verification (single PR)",
        "detail": "run one PR's tests red→green in the Docker sandbox. Needs the sandbox provisioned on this machine.",
        "agentic": True,
        "ledger": ("pr", ("verify:single",)),
        "needs_pr": True,
        "argv_fn": lambda pr: [*PIPELINE_PY, "-u",
                               str(REPO_ROOT / "pipeline" / "verify_pr.py"),
                               "--pr", str(pr)],
    },
    "threat-scan-pr": {
        "label": "Threat scan (single PR)",
        "detail": "the deterministic attack-pattern scan over one PR's diff.",
        "agentic": False,
        "needs_pr": True,
        "argv_fn": lambda pr: [*PIPELINE_PY, "-u",
                               str(REPO_ROOT / "pipeline" / "threat_scan.py"),
                               "--only", str(pr)],
    },
    "selftest": {
        "label": "Self-test (echo)",
        "detail": "a trivial echo that proves the job-streaming path works.",
        "agentic": False,
        "argv": [*PIPELINE_PY, "-u", "-c",
                 "import time\nfor i in range(5):\n    print(f'tick {i}', flush=True)\n    time.sleep(0.3)\nprint('done')"],
    },
}

JOBS: dict[int, Job] = {}
_TASKS: set[asyncio.Task[None]] = set()
# Async primitives belong to their event loop. The app uses one loop while
# TestClient and unit tests may create additional loops in the same process.
_LIMITERS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]
] = WeakKeyDictionary()


def list_specs() -> list[JobSpecView]:
    return [{"kind": k, "label": v["label"], "detail": v["detail"], "agentic": v["agentic"],
             "needs_cluster": v.get("needs_cluster", False),
             "needs_pr": v.get("needs_pr", False), "needs_count": v.get("needs_count", False),
             "count_default": v.get("count_default"), "count_noun": v.get("count_noun")}
            for k, v in JOB_SPECS.items()]


def view(job: Job) -> JobView:
    return {"id": job["id"], "kind": job["kind"], "cluster": job["cluster"],
            "pr": job["pr"], "count": job["count"], "status": job["status"],
            "label": job["label"], "started": job["started"],
            "finished": job["finished"], "returncode": job["returncode"]}


def list_jobs() -> list[JobView]:
    return [view(job) for job in sorted(JOBS.values(), key=lambda item: item["id"], reverse=True)]


def _record_path(job_id: int) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _log_path(job_id: int) -> Path:
    return JOBS_DIR / f"{job_id}.log"


def _exit_path(job_id: int) -> Path:
    return JOBS_DIR / f"{job_id}.exit"


def _now() -> str:
    return storekit.now()


def _save(job: Job) -> None:
    record = {**view(job), "pid": job["pid"], "stop_requested": job["stop_requested"],
              "argv": job["_argv"]}
    path = _record_path(job["id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(path)


def _recorded_ids() -> list[int]:
    if not JOBS_DIR.is_dir():
        return []
    return [int(p.stem) for p in JOBS_DIR.glob("*.json") if p.stem.isdigit()]


def _claim_id() -> int:
    """The next job id, reserved by creating its record file exclusively, so ids
    keep rising across restarts and two backends on one checkout never share one."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    n = max([0, *JOBS, *_recorded_ids()]) + 1
    while True:
        try:
            os.close(os.open(_record_path(n), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return n
        except FileExistsError:
            n += 1


def _forget(job_id: int) -> None:
    JOBS.pop(job_id, None)
    for path in (_record_path(job_id), _log_path(job_id), _exit_path(job_id)):
        path.unlink(missing_ok=True)


def _prune() -> None:
    finished = sorted(j["id"] for j in JOBS.values() if j["status"] in ("done", "failed"))
    for job_id in finished[:max(0, len(finished) - KEEP_FINISHED)]:
        _forget(job_id)


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
            and j["status"] in ("queued", "running")
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
    job: Job = {
        "id": _claim_id(), "kind": kind, "cluster": cluster, "pr": pr, "count": count,
        "label": spec["label"], "status": "queued", "log": [],
        "started": _now(), "finished": None, "returncode": None,
        "pid": None, "stop_requested": False,
        "_argv": argv, "_wake": asyncio.Event(), "_log_read": 0,
    }
    # A log or exit file left by a record deleted by hand is not this job's.
    _log_path(job["id"]).unlink(missing_ok=True)
    _exit_path(job["id"]).unlink(missing_ok=True)
    JOBS[job["id"]] = job
    _save(job)
    _prune()
    return job


def _notify(job: Job) -> None:
    """Wake attached SSE readers. Each waiter retains its current Event while the
    job installs the Event for the next change."""
    job["_wake"].set()
    job["_wake"] = asyncio.Event()


def _append(job: Job, line: str) -> None:
    """Append one runner line to the job's log file, on a line of its own even
    when the child's last output did not end its line."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with _log_path(job["id"]).open("a+b") as fh:
        size = fh.seek(0, os.SEEK_END)
        lead = b""
        if size:
            fh.seek(size - 1)
            if fh.read(1) != b"\n":
                lead = b"\n"
        fh.write(lead + line.encode("utf-8", "replace") + b"\n")


def _catch_up(job: Job, final: bool = False) -> None:
    """Split up to CATCH_UP_BYTES of the log file's new complete lines into
    `log` (everything left, when `final`) and wake readers. A trailing partial
    line waits for its newline unless `final` or it fills a whole read. A
    carriage return keeps only what follows it, as a terminal would show."""
    try:
        with _log_path(job["id"]).open("rb") as fh:
            fh.seek(job["_log_read"])
            chunk = fh.read() if final else fh.read(CATCH_UP_BYTES)
    except FileNotFoundError:
        return
    if not chunk:
        return
    lines = chunk.split(b"\n")
    tail = lines.pop()
    if tail and (final or not lines):
        lines.append(tail)
        tail = b""
    job["_log_read"] += len(chunk) - len(tail)
    added = False
    for raw in lines:
        text = raw.decode("utf-8", "replace").rstrip("\r").rsplit("\r", 1)[-1]
        if text.strip():
            job["log"].append(text)
            added = True
    if added:
        _notify(job)


def _recorded_exit(job_id: int) -> int | None:
    try:
        return int(_exit_path(job_id).read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _runs_job(job: Job) -> bool | None:
    """Whether the job's recorded pid is still the wrapper spawned for it — its
    command line names this job's exit file — and not a process that reused the
    pid. None when the pid is alive but `ps` cannot say."""
    pid = job["pid"]
    if pid is None or not _alive(pid):
        return False
    try:
        out = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return str(_exit_path(job["id"])) in out


def _process_groups_under(pid: int) -> set[int]:
    """The process groups of `pid` and of every process descended from it. A
    job's headless agents and sandbox launchers start sessions of their own, so
    its own group does not reach them."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {pid}
    children: dict[int, list[int]] = {}
    group_of: dict[int, int] = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) != 3 or not all(f.isdigit() for f in fields):
            continue
        child, parent, group = (int(f) for f in fields)
        children.setdefault(parent, []).append(child)
        group_of[child] = group
    groups: set[int] = set()
    stack = [pid]
    while stack:
        proc = stack.pop()
        if proc in group_of:
            groups.add(group_of[proc])
        stack.extend(children.get(proc, []))
    return groups or {pid}


def _signal_groups(groups: set[int], sig: signal.Signals) -> None:
    for group in groups:
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError):
            pass


async def _kill_after_grace(groups: set[int]) -> None:
    await asyncio.sleep(STOP_GRACE_SECONDS)
    _signal_groups(groups, signal.SIGKILL)


def _settle(job: Job, returncode: int | None, note: str | None = None) -> None:
    """Record the job's ending: its last lines, status, and record."""
    if note:
        _append(job, note)
    _catch_up(job, final=True)
    job["returncode"] = returncode
    job["status"] = "done" if returncode == 0 else "failed"
    job["finished"] = _now()
    _save(job)


async def _complete(job: Job, returncode: int | None, note: str | None = None) -> None:
    """Settle the job once the snapshot reflects what it wrote to the store,
    saying so in the log, since over a slow link that refresh takes a while."""
    if note:
        _append(job, note)
    ended = f"exit {returncode}" if returncode is not None else "no exit code"
    _append(job, f"· process ended ({ended}); refreshing the app's data before marking the job finished…")
    _catch_up(job)
    try:
        await asyncio.to_thread(data.refresh)
    except Exception as e:
        _append(job, f"! refreshing the app's data after the job failed: {e}")
    _settle(job, returncode)
    _notify(job)


async def _follow(job: Job, proc: subprocess.Popen[bytes] | None) -> None:
    """Stream the job's log file into `log` until its process exits, then settle
    it. `proc` is this backend's own child; None for a job adopted after a restart."""
    pid = job["pid"]
    assert pid is not None
    while True:
        _catch_up(job)
        if _recorded_exit(job["id"]) is not None:
            break
        if (proc.poll() is not None) if proc is not None else not _alive(pid):
            break
        await asyncio.sleep(POLL_SECONDS)
    if proc is not None:
        while proc.poll() is None:
            await asyncio.sleep(POLL_SECONDS)
    returncode = _recorded_exit(job["id"])
    if job["stop_requested"] and job["kind"] == "verify-pr":
        await _remove_orphaned_sandboxes(job)
    if returncode is not None:
        await _complete(job, returncode)
    elif job["stop_requested"]:
        await _complete(job, proc.returncode if proc is not None else None,
                        "■ stopped by the operator")
    else:
        await _complete(job, proc.returncode if proc is not None else None,
                        "! the job's process ended without recording an exit code")


async def _remove_orphaned_sandboxes(job: Job) -> None:
    """Remove the sandbox containers a stopped verify job left running: their
    host process is gone, and its timeout went with it."""
    from pipeline import verify_driver
    try:
        removed = await asyncio.to_thread(verify_driver.stop_orphaned_sandboxes)
    except Exception as e:
        _append(job, f"! removing the job's sandbox containers failed: {e}")
        return
    if removed:
        _append(job, f"■ removed {len(removed)} sandbox container(s) the job left running")


async def run_job(job: Job) -> None:
    """Drive `job`'s subprocess to completion. Scheduled once via
    `asyncio.create_task` right after `start_job`, independent of any SSE
    reader — the job runs to completion (updating `log`/`status`/`returncode`
    on `job` throughout) whether or not a client is attached, so navigating
    away from the page that started it neither stops nor orphans it (#683).
    The child writes to the job's log file in its own session; this task only
    reads that file, so cancelling it leaves the child running for `restore`."""
    if job["status"] != "queued":
        return
    argv = job["_argv"]
    job["status"] = "running"
    _save(job)
    _append(job, f"$ {' '.join(str(a)[:60] for a in argv[:4])}…")
    _catch_up(job)
    try:
        with _log_path(job["id"]).open("ab") as log:
            proc = subprocess.Popen(
                ["sh", "-c", _EXIT_WRAPPER, "sh", str(_exit_path(job["id"])), *argv],
                cwd=REPO_ROOT, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True,
                env={**os.environ, progress.ENV: "1"})
    except OSError as e:
        await _complete(job, -1, f"! job crashed: {e}")
        return
    job["pid"] = proc.pid
    _save(job)
    await _follow(job, proc)


def _from_record(record: dict) -> Job | None:
    try:
        job: Job = {
            "id": int(record["id"]), "kind": str(record["kind"]),
            "cluster": record.get("cluster"), "pr": record.get("pr"),
            "count": record.get("count"), "label": str(record["label"]),
            "status": record["status"], "started": str(record["started"]),
            "finished": record.get("finished"), "returncode": record.get("returncode"),
            "pid": record.get("pid"), "stop_requested": bool(record.get("stop_requested")),
            "log": [], "_argv": [str(a) for a in record["argv"]],
            "_wake": asyncio.Event(), "_log_read": 0,
        }
    except (KeyError, TypeError, ValueError):
        return None
    if job["status"] not in ("queued", "running", "done", "failed"):
        return None
    return job


def restore() -> None:
    """Load the jobs recorded on disk that this backend does not hold, and take
    each unfinished one back: a running job whose process is still going is
    followed again, one whose exit code was recorded is settled, one whose
    process is gone without an exit code is failed, and a queued one is scheduled.
    Runs on the event loop, which the followed and scheduled jobs need."""
    for job_id in sorted(_recorded_ids()):
        if job_id in JOBS:
            continue
        try:
            record = json.loads(_record_path(job_id).read_text())
        except (OSError, ValueError):
            continue
        job = _from_record(record) if isinstance(record, dict) else None
        if job is None:
            continue
        JOBS[job_id] = job
        if job["status"] == "queued":
            schedule_job(job)
        elif job["status"] == "running":
            returncode = _recorded_exit(job_id)
            if returncode is not None:
                _settle(job, returncode)
            elif _runs_job(job) is not False:
                _retain(asyncio.create_task(_follow(job, None)))
            else:
                _settle(job, None, "! lost: the job's process ended while the backend was "
                                   "restarting, without recording an exit code")


async def stop_job(job_id: int) -> Job:
    """Stop a queued or running job: a queued one never starts, and every
    process group in a running one's process tree gets SIGTERM, then SIGKILL
    after STOP_GRACE_SECONDS. Raises KeyError for an unknown job, ValueError for
    a finished one or one whose process cannot be confirmed as the job's."""
    job = JOBS[job_id]
    if job["status"] == "queued":
        job["stop_requested"] = True
        _settle(job, None, "■ stopped by the operator before it started")
        _notify(job)
        return job
    if job["status"] != "running":
        raise ValueError(f"job {job_id} already {job['status']}")
    pid = job["pid"]
    if pid is None or await asyncio.to_thread(_runs_job, job) is not True:
        raise ValueError(f"job {job_id}'s process could not be confirmed as the job's")
    job["stop_requested"] = True
    _save(job)
    groups = await asyncio.to_thread(_process_groups_under, pid)
    _append(job, f"■ stopping: sent SIGTERM to the job's {len(groups)} process group(s); "
                 f"SIGKILL follows in {STOP_GRACE_SECONDS:.0f}s for any still running…")
    _catch_up(job)
    _signal_groups(groups, signal.SIGTERM)
    _retain(asyncio.create_task(_kill_after_grace(groups)))
    return job


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


def _retain(task: asyncio.Task[None]) -> None:
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def schedule_job(job: Job) -> None:
    """Schedule one job and retain its task through completion."""
    _retain(asyncio.create_task(_run_scheduled_job(job)))


def _meta(job: Job) -> dict[str, object]:
    """The job's view, when its log last changed, and how many lines it holds —
    what a console shows above the output, and how it tells replayed lines from
    live ones."""
    try:
        last_output: str | None = datetime.fromtimestamp(
            _log_path(job["id"]).stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        last_output = None
    return {**view(job), "last_output": last_output, "lines": len(job["log"])}


async def attach_job(job: Job, after: int = 0) -> AsyncIterator[dict[str, str]]:
    """Stream `job`'s log as SSE — a `job` event with its view first, then every
    line from index `after` on (a reconnecting reader passes the count it already
    has), then following live, then a final `done` event. Safe to call more than
    once per job and safe to abandon at any point: closing this generator only
    drops this reader, the job keeps going."""
    _catch_up(job, final=job["status"] in ("done", "failed"))
    yield {"event": "job", "data": json.dumps(_meta(job))}
    i = max(0, after)
    while True:
        while i < len(job["log"]):
            yield {"event": "log", "data": job["log"][i]}
            i += 1
        if job["status"] not in ("queued", "running"):
            break
        await job["_wake"].wait()
    yield {"event": "done", "data": json.dumps({"returncode": job["returncode"],
                                                "status": job["status"],
                                                "finished": job["finished"]})}


async def attach_job_group(group: list[Job]) -> AsyncIterator[dict[str, str]]:
    """Stream status changes for several jobs over one SSE connection.

    The initial state of every job is replayed, so attaching after a fast job
    finishes is safe. Job logs stay on the per-job stream; this connection
    carries the lifecycle updates bulk callers need.
    """
    previous: dict[int, tuple[JobStatus, int | None]] = {}
    while True:
        # Capture each current Event before reading state. An emit racing this
        # snapshot sets the captured Event, so the next pass cannot miss it.
        wakes = [job["_wake"] for job in group]
        active = False
        for job in group:
            state = (job["status"], job["returncode"])
            if previous.get(job["id"]) != state:
                previous[job["id"]] = state
                yield {"event": "job", "data": json.dumps({
                    "id": job["id"], "status": job["status"],
                    "returncode": job["returncode"],
                })}
            active = active or job["status"] in ("queued", "running")
        if not active:
            break
        waiters = [asyncio.create_task(wake.wait()) for wake in wakes]
        _, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for waiter in pending:
            waiter.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    yield {"event": "done", "data": json.dumps({"jobs": len(group)})}
