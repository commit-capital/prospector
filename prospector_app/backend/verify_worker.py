"""The sandbox-verification worker: drains queued verify_requests by running
pipeline/verify_pr.py, one PR at a time, inside the app backend process.

It runs ONLY where TRIAGE_VERIFY_WORKER=1 is set — the machine with the Docker
sandbox and the pinned base image. Every other app backend serves the same
queue/dequeue API but starts no worker; the queue lives in the shared store,
so a click anywhere reaches the runner here within one poll tick.

Two daemon threads: a heartbeat (writes the verify_worker registry every tick,
so any app can show runner liveness) and the drain loop (orphan recovery,
then pick-oldest-queued → spawn the orchestrator → finalize). The orchestrator
owns the request's transitions; the worker only recovers what a dead process
left behind.

With TRIAGE_VERIFY_AUTOHUNT=1 an empty queue turns the drain loop into a
hunter: it runs the headless security review on clean merge candidates that
lack a current verdict (highest community pain first), and once none remain,
auto-queues sandbox verification for candidates whose security verdict is a
current GREEN — the sandbox never executes code the adversarial review has
not cleared. Selection is pure gate policy; an operator-queued request always
wins the next pick.
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pipeline import gates
from pipeline import verify_driver
from pipeline.freshness import is_current
from pipeline.storekit import now as _now
from prospector_app.backend import data
from prospector_app.backend import service
from prospector_app.backend import verify_queue
from prospector_app.backend.jobs import PIPELINE_PY, REPO_ROOT

if TYPE_CHECKING:
    from pipeline.model import Pr
    from pipeline.store import Store

POLL_SECONDS = 15.0

# How long a `waiting-for-base` request rests between attempts: it is eligible
# for pickup again only once its last write (checked_at) is at least this old,
# so a missing base costs one cheap preflight per rest interval, not one per
# poll tick.
BASE_RETRY_SECONDS = 60.0

# A transiently re-queued request (a verify run that died on an upstream fetch,
# a headless agent, or the sandbox — verify_pr re-queues it with an attempt
# count) rests this long before pickup, giving the failing dependency time to
# recover instead of burning the attempt cap in one bad minute.
TRANSIENT_RETRY_SECONDS = 600.0

# How old the base pin must be before the daily refresh considers re-pinning.
REFRESH_AFTER_HOURS = 24.0

# The steps a restart may resume: at each of them the run has committed nothing
# and no phase container has run the PR's code, so re-queueing repeats only
# cheap work. "claimed" is the store's own pickup stamp, written before the
# orchestrator starts. From "sandbox" on, the expensive half has already
# executed.
RESUMABLE_STEPS = ("claimed", "preflight", "blind", "author")

# How many restarts a request may be resumed through. Past the cap the
# interruption is the operator's to look at: a worker that keeps dying in the
# same pre-sandbox step is a failure of its own, not a run to re-fire.
RESTART_MAX_ATTEMPTS = 3

# Last captured orchestrator output kept on a request the worker itself has to
# mark errored (the orchestrator died without writing a terminal status).
TAIL_CHARS = 4000

# The PR the drain loop is currently running (None when idle) — read by the
# heartbeat thread; `stop` ends both loops (tests set it).
state: dict[str, int | None] = {"current_pr": None}
stop = threading.Event()


def enabled() -> bool:
    """Whether THIS backend is the verification runner. Deliberately an exact
    opt-in: only the machine whose .env sets TRIAGE_VERIFY_WORKER=1 (the Mac
    Studio with the sandbox) drains the queue."""
    return os.environ.get("TRIAGE_VERIFY_WORKER") == "1"


def beat() -> None:
    """Write this worker's liveness, autohunt opt-in, and security failure
    memory into the shared store."""
    data.store().save_verify_worker({
        "host": socket.gethostname(), "pid": os.getpid(),
        "last_beat": _now(), "current_pr": state["current_pr"],
        "autohunt": enabled_autohunt(), "security_failed": sorted(security_failed)})


def recover_orphans() -> tuple[list[int], list[int]]:
    """Resolve THIS host's `running` requests: a run does not survive the
    worker's process, so at startup a running status claimed by this host can
    only be a restart's leftover. A request another host claimed is its live
    run, never touched from here; a hostless record (written before claims
    carried a host) is treated as this host's.

    A run stopped at a RESUMABLE_STEPS step re-queues itself, capped at
    RESTART_MAX_ATTEMPTS: nothing of the PR's verification ran. Anything further
    along is marked `interrupted` for a deliberate re-queue — a heavy sandbox
    run is never silently re-fired. Returns (marked, requeued)."""
    marked: list[int] = []
    requeued: list[int] = []
    me = socket.gethostname()
    st = data.store()
    with st.batch():
        for n, rec in sorted(st.all_prs().items()):
            req = rec.verify_request or {}
            if req.get("status") != "running":
                continue
            if req.get("host") not in (None, me):
                continue
            attempts = req.get("attempts") or 0
            if req.get("step") in RESUMABLE_STEPS and attempts < RESTART_MAX_ATTEMPTS:
                st.edit_pr(n).record_verify_request(
                    "queued", queued_at=req.get("queued_at"),
                    error_kind="interrupted",
                    error="the verification worker restarted before the sandbox "
                          "ran — re-queued automatically",
                    attempts=attempts + 1, source=req.get("source"), host=me)
                requeued.append(n)
                continue
            st.edit_pr(n).record_verify_request(
                "error", queued_at=req.get("queued_at"),
                started_at=req.get("started_at"), finished_at=_now(),
                error_kind="interrupted",
                error="the verification worker restarted mid-run — re-queue to retry",
                source=req.get("source"), host=me)
            marked.append(n)
    return marked, requeued


def _rested(req: dict, seconds: float) -> bool:
    """Whether a parked request's last attempt (its checked_at write stamp) is
    at least `seconds` old. A missing or unparseable stamp reads as rested, so
    a malformed record cannot wait forever."""
    stamp = req.get("checked_at")
    if not isinstance(stamp, str):
        return True
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - at).total_seconds() >= seconds


def base_refresh_due(reg: dict, now: datetime) -> bool:
    """Whether the daily pin refresh should attempt now: a pin exists, is
    older than REFRESH_AFTER_HOURS, and no attempt was made on today's date
    (one attempt per day, success or failure). A machine without a pin never
    builds one from here — the first pin is the operator's prepare-base."""
    pinned_at = reg.get("pinned_at")
    if not reg.get("base_sha") or not isinstance(pinned_at, str):
        return False
    try:
        pinned = datetime.fromisoformat(pinned_at)
    except ValueError:
        return False
    if (now - pinned).total_seconds() < REFRESH_AFTER_HOURS * 3600:
        return False
    attempted = reg.get("refresh_attempted_at")
    if isinstance(attempted, str):
        try:
            if datetime.fromisoformat(attempted).date() == now.date():
                return False
        except ValueError:
            pass
    return True


def _record_refresh(st: Store, ok: bool, error: str | None, failures: int) -> None:
    """Stamp the refresh outcome onto the pin: whether the last attempt
    succeeded, its error, the consecutive-failure run, and the once-per-day
    attempt stamp. Written after prepare_base returns, because prepare_base
    full-replaces the registry with the fields it owns."""
    st.save_verify_base({
        **st.load_verify_base(), "refresh_attempted_at": _now(), "refresh_ok": ok,
        "refresh_error": error, "refresh_failures": 0 if ok else failures + 1})


def maybe_refresh_base() -> None:
    """The daily pin refresh: when the pin is a day old and upstream's default
    branch has moved, re-run prepare_base so verification tracks master within
    ~24h. The attempt stamps refresh_attempted_at BEFORE the work at most once
    per calendar day (success or failure); a failure keeps the old pin and the
    queue proceeds on it; every attempt lands in the runs ledger.

    The outcome is also stamped onto the pin itself, where the app reads it: a
    lane that has silently stopped tracking master is worth showing, and a
    ledger entry is not a surface anyone watches. An unmoved default branch is a
    healthy refresh — there was nothing to move to."""
    try:
        st = data.store()
        reg = st.load_verify_base()
        if not base_refresh_due(reg, datetime.now(timezone.utc)):
            return
        failures = reg.get("refresh_failures")
        failures = failures if isinstance(failures, int) else 0
        st.save_verify_base({**reg, "refresh_attempted_at": _now()})
        entry: dict = {"phase": "verify:pin-refresh", "started": _now(),
                       "stats": {"from": str(reg.get("base_sha"))[:12]}}
        try:
            sha = verify_driver.resolve_base_sha()
            if sha == reg.get("base_sha"):
                entry["stats"].update(ok=True, unmoved=True)
                _record_refresh(st, True, None, failures)
                return
            print(f"[verify-worker] daily pin refresh: "
                  f"{str(reg.get('base_sha'))[:12]} -> {sha[:12]}", flush=True)
            verify_driver.prepare_base(st, base_sha=sha, tier=int(reg.get("tier", 1)))
            _record_refresh(st, True, None, failures)
            entry["stats"].update(ok=True, to=sha[:12])
        except Exception as e:
            detail = f"{type(e).__name__}: {str(e)[:200]}"
            entry["stats"].update(ok=False, error=detail)
            _record_refresh(st, False, detail, failures)
            traceback.print_exc()
        finally:
            entry["finished"] = _now()
            st.append_run(entry)
    except Exception:
        traceback.print_exc()


def next_queued() -> int | None:
    """The best runnable PR, or None: every `queued` request (a transiently
    re-queued one — a nonzero `attempts` — only once rested since its last
    attempt), plus each `waiting-for-base` request rested since its last
    attempt, ranked operator picks before auto-picks and, within each group,
    oldest queued_at first — so an operator click never waits behind an
    earlier auto-queued request. Reads the backend's incremental store
    snapshot, so the scan costs no store round-trip.

    A parked base-waiter is held back while the Docker daemon is down, since
    its preflight reaches that same verdict. The probe costs one call per scan
    and is consulted only once a rested base-waiter is in hand; a `queued`
    request is never gated by it, so its preflight is what records why it
    cannot proceed."""
    best_n: int | None = None
    best_key: tuple[bool, str] | None = None
    daemon: bool | None = None
    for n, rec in data.prs().items():
        req = rec.verify_request or {}
        status = req.get("status")
        if status == "waiting-for-base":
            if not _rested(req, BASE_RETRY_SECONDS):
                continue
            if daemon is None:
                daemon = verify_driver.daemon_available()
            if not daemon:
                continue
        elif status == "queued":
            if req.get("attempts") and not _rested(req, TRANSIENT_RETRY_SECONDS):
                continue
        else:
            continue
        key = (req.get("source") == "auto", str(req.get("queued_at") or ""))
        if best_key is None or key < best_key:
            best_n, best_key = n, key
    return best_n


# Security auto-runs that exited nonzero this process: the hunter skips them
# so a broken run is never immediately re-fired (a backend restart clears the
# set; the operator can re-run any PR from the app meanwhile).
security_failed: set[int] = set()


def enabled_autohunt() -> bool:
    """Whether the idle auto-hunt runs on this backend. An exact opt-in like
    enabled(), and meaningful only alongside it."""
    return os.environ.get("TRIAGE_VERIFY_AUTOHUNT") == "1"


def _changed_paths(pr: Pr) -> list[str]:
    """The PR's changed paths from its cached diff, [] when none is cached —
    fail-closed: no paths means not verify-eligible."""
    return service.changed_paths(pr)


def _pain(pr: Pr) -> float:
    return service.pr_pain(pr)["score"]


def auto_verifiable(pr: Pr) -> bool:
    """Whether the hunter may queue a sandbox run: verify-eligible with a
    current GREEN security verdict (the sandbox never executes code the
    adversarial review has not cleared), no current verify record, and no
    verify_request in flight or deliberately stopped (error/cancelled wait
    for an operator re-queue)."""
    status = (pr.verify_request or {}).get("status")
    if status in ("queued", "running", "waiting-for-base", "error", "cancelled"):
        return False
    if not gates.security_cleared(pr):
        return False
    if is_current(pr, "verify", max_age_days=gates.VERIFY_MAX_AGE_DAYS):
        return False
    return gates.verify_eligible(pr, _changed_paths(pr))


def next_auto() -> tuple[str, int] | None:
    """The idle hunt's next pick, or None: ("security", n) while any clean
    merge candidate lacks a current security verdict, else ("verify", n) for
    the best GREEN-cleared unverified candidate. Both lanes order by highest
    community pain, then lowest PR number."""
    prs = data.prs()

    def _key(item: tuple[int, Pr]) -> tuple[float, int]:
        return (-_pain(item[1]), item[0])

    security_pool = [(n, pr) for n, pr in prs.items()
                     if n not in security_failed and gates.blocked_on_security(pr)]
    if security_pool:
        return ("security", min(security_pool, key=_key)[0])
    verify_pool = [(n, pr) for n, pr in prs.items() if auto_verifiable(pr)]
    if verify_pool:
        return ("verify", min(verify_pool, key=_key)[0])
    return None


def run_security(n: int) -> int:
    """Run the headless per-PR security review for PR `n` and return its exit
    code. A PR stays in security_failed until a run both exits 0 and leaves it
    outside the security pool: pipeline/security_review.py can exit 0 while
    holding an INCOMPLETE review (a lens agent failure) without writing a
    verdict, so the exit code alone does not prove a verdict landed. A fresh
    pre-spawn recheck skips a PR another
    process already cleared since it was picked (snapshot lag). The runner
    owns the verdict write, any RED flip, and the runs-ledger entry."""
    rec = data.store().load_pr(n)
    if rec is None or not gates.blocked_on_security(rec):
        return 0
    security_failed.add(n)
    argv = [*PIPELINE_PY, "-u", str(REPO_ROOT / "pipeline" / "security_review.py"),
            "--pr", str(n), "--trigger", "autohunt"]
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        print(f"[autohunt pr {n}] {line}", end="", flush=True)
    rc = proc.wait()
    data.refresh()
    rec2 = data.store().load_pr(n)
    if rc == 0 and rec2 is not None and not gates.blocked_on_security(rec2):
        security_failed.discard(n)
    return rc


def auto_queue_verify(n: int) -> None:
    """Queue PR `n` for sandbox verification as an auto-pick. The drain loop
    runs it on its next pick exactly like an operator-queued request."""
    print(f"[autohunt] queueing PR #{n} for verification", flush=True)
    verify_queue.queue_pr(n, source="auto")


def run_one(n: int) -> int:
    """Run the orchestrator for PR `n` and return its exit code. The
    orchestrator writes the request's transitions and the verify record; the
    worker echoes its output to the backend log and finalizes only a request
    the orchestrator's death left non-terminal."""
    argv = [*PIPELINE_PY, "-u", str(REPO_ROOT / "pipeline" / "verify_pr.py"),
            "--pr", str(n), "--from-queue"]
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    lines: list[str] = []
    for line in proc.stdout:
        print(f"[verify-worker pr {n}] {line}", end="", flush=True)
        lines.append(line)
        if len(lines) > 200:
            del lines[:100]
    rc = proc.wait()
    _finalize(n, rc, "".join(lines)[-TAIL_CHARS:])
    return rc


def _finalize(n: int, rc: int, tail: str) -> None:
    """A pickup must end terminal: if the orchestrator exited with the request
    still `queued`/`running` (it crashed before recording a result), mark it
    errored with the captured output — a pending leftover would be picked up
    again next tick and crash-loop. A request another host claimed is left
    alone: this pickup lost the claim race, and the running status is the
    winner's live run."""
    me = socket.gethostname()
    st = data.store()
    rec = st.load_pr(n)
    if rec is not None:
        req = rec.verify_request or {}
        if (req.get("status") in ("queued", "running")
                and req.get("host") in (None, me)):
            st.edit_pr(n).record_verify_request(
                "error", queued_at=req.get("queued_at"),
                started_at=req.get("started_at"), finished_at=_now(),
                error_kind="exception",
                error=f"verify_pr exited {rc} without recording a result",
                log_tail=tail or None, source=req.get("source"), host=me)
    data.refresh()


def _beat_loop() -> None:
    while not stop.is_set():
        try:
            beat()
        except Exception:
            traceback.print_exc()
        stop.wait(POLL_SECONDS)


def _drain_loop() -> None:
    try:
        marked, requeued = recover_orphans()
        if marked:
            print(f"[verify-worker] marked interrupted after restart: {marked}", flush=True)
        if requeued:
            print(f"[verify-worker] re-queued after restart: {requeued}", flush=True)
    except Exception:
        traceback.print_exc()
    while not stop.is_set():
        try:
            maybe_refresh_base()
            n = next_queued()
            if n is not None:
                state["current_pr"] = n
                beat()
                print(f"[verify-worker] picking up PR #{n}", flush=True)
                run_one(n)
                continue
            pick = next_auto() if enabled_autohunt() else None
            if pick is None:
                stop.wait(POLL_SECONDS)
                continue
            lane, n = pick
            if lane == "security":
                state["current_pr"] = n
                beat()
                print(f"[autohunt] security review for PR #{n}", flush=True)
                run_security(n)
            else:
                auto_queue_verify(n)
        except Exception:
            traceback.print_exc()
            stop.wait(POLL_SECONDS)
        finally:
            state["current_pr"] = None


def startup() -> bool:
    """Start the heartbeat + drain threads when this backend is the runner.
    Returns whether the worker started."""
    if not enabled():
        return False
    threading.Thread(target=_beat_loop, daemon=True, name="verify-worker-beat").start()
    threading.Thread(target=_drain_loop, daemon=True, name="verify-worker").start()
    print(f"[verify-worker] enabled on {socket.gethostname()} "
          f"(poll every {POLL_SECONDS:.0f}s)", flush=True)
    return True
