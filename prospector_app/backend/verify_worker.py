"""The sandbox-verification worker: drains queued verify_requests by running
pipeline/verify_pr.py, one PR at a time, inside the app backend process.

It runs where TRIAGE_VERIFY_WORKER=1 is set — a machine with the Docker sandbox
and its own pinned base image. Every other app backend serves the same
queue/dequeue API but starts no worker; the queue lives in the shared store, so
a click anywhere reaches a runner within one poll tick. Any number of machines
may run one: the store's claim is a compare-and-swap, so two workers never pick
up the same PR.

Two daemon threads: a heartbeat (writes the verify_worker registry every tick,
so any app can show runner liveness) and the drain loop (orphan recovery,
then pick-oldest-queued → spawn the orchestrator → finalize). The orchestrator
owns the request's transitions; the worker only recovers what a dead process
left behind.

With TRIAGE_VERIFY_AUTOHUNT=1 an empty queue turns the drain loop into a
hunter: it runs the headless security review on clean merge candidates that
lack a current verdict (highest community pain first), then auto-queues
sandbox verification for candidates whose security verdict is a current GREEN
— the sandbox never executes code the adversarial review has not cleared —
and finally re-sweeps concluded verifications whose independent repro the
harness itself broke, once each. Selection is pure gate policy; an
operator-queued request always wins the next pick.
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
from pipeline import store
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

# How long another machine's claim on a security review is honoured. A review is
# several agent runs deep, each capped at headless_agent's own timeout, so this
# sits well past a slow one — it exists to free a PR whose machine died, not to
# overtake one still working.
SECURITY_CLAIM_SECONDS = 4 * 3600.0

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


# This backend's live worker threads. A restart reads them to tell a running
# worker from a stopped one, so a flag toggled twice never leaves two drain
# loops racing each other for pickups.
_threads: list[threading.Thread] = []

# How long shutdown waits for the loops to notice. A loop resting between polls
# ends at once; one inside a run finishes it first, so a caller that times out
# has signalled a stop, not failed to.
SHUTDOWN_TIMEOUT = 5.0


def running() -> bool:
    """Whether this backend's worker threads are alive."""
    return any(t.is_alive() for t in _threads)


def shutdown(timeout: float = SHUTDOWN_TIMEOUT) -> bool:
    """Signal the loops to end and wait up to `timeout` for them. Returns
    whether they are stopped — False means the stop is signalled and the work in
    flight is still finishing, which is not a failure."""
    if not running():
        _threads.clear()
        return True
    stop.set()
    for t in _threads:
        t.join(timeout=timeout)
    if running():
        return False
    _threads.clear()
    return True


def enabled() -> bool:
    """Whether THIS backend drains the verification queue. Deliberately an exact
    opt-in: a machine needs the Docker sandbox and its own prepared base, so it
    says so in its own .env."""
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
    """Whether this machine's daily pin refresh should attempt now: its pin
    exists, is older than REFRESH_AFTER_HOURS, and no attempt was made on
    today's date (one attempt per day, success or failure). A machine without a
    pin never builds one from here — the first pin is the operator's
    prepare-base."""
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
    """Stamp the refresh outcome onto this machine's pin: whether the last
    attempt succeeded, its error, the consecutive-failure run, and the
    once-per-day attempt stamp. Written after prepare_base returns, because
    prepare_base full-replaces this host's record with the fields it owns."""
    me = socket.gethostname()
    st.save_verify_base({
        **st.load_verify_base(me), "host": me,
        "refresh_attempted_at": _now(), "refresh_ok": ok,
        "refresh_error": error, "refresh_failures": 0 if ok else failures + 1})


def maybe_refresh_base() -> None:
    """The daily pin refresh: when this machine's pin is a day old and
    upstream's default branch has moved, re-run prepare_base so verification
    tracks master within ~24h. Each verification machine holds its own pin and
    refreshes on its own cadence, so two machines sit a few hours apart at
    worst; every run records the base it used (verify.against_base_sha). The
    attempt stamps refresh_attempted_at BEFORE the work at most once per
    calendar day (success or failure); a failure keeps the old pin and the queue
    proceeds on it; every attempt lands in the runs ledger.

    The outcome is also stamped onto the pin itself, where the app reads it: a
    lane that has silently stopped tracking master is worth showing, and a
    ledger entry is not a surface anyone watches. An unmoved default branch is a
    healthy refresh — there was nothing to move to."""
    try:
        st = data.store()
        me = socket.gethostname()
        reg = st.load_verify_base(me)
        if not base_refresh_due(reg, datetime.now(timezone.utc)):
            return
        failures = reg.get("refresh_failures")
        failures = failures if isinstance(failures, int) else 0
        st.save_verify_base({**reg, "host": me, "refresh_attempted_at": _now()})
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
        key = (req.get("source") in store.AUTO_REQUEST_SOURCES,
               str(req.get("queued_at") or ""))
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


# The `source` stamped on a re-sweep request. It is also the terminator: a PR
# whose verify record was written after its own re-sweep request never re-sweeps
# again, so one broken repro buys exactly one extra sandbox run per head.
RESWEEP_SOURCE = "auto-resweep"


def _resweep_spent(pr: Pr) -> bool:
    """Whether this PR's current verify record already came out of a re-sweep:
    the last request was one, and the record was written at or after it was
    queued. A head that moves writes a fresh record and clears the ledger with
    it, so the one retry is per head, not per PR forever."""
    req = pr.verify_request or {}
    if req.get("source") != RESWEEP_SOURCE:
        return False
    queued = str(req.get("queued_at") or "")
    checked = str((pr.section("verify") or {}).get("checked_at") or "")
    return bool(queued and checked and checked >= queued)


def auto_resweepable(pr: Pr) -> bool:
    """Whether the hunter may re-queue a PR whose verification concluded but
    whose independent repro the harness broke (gates.repro_harness_defect).

    Rules out everything auto_verifiable rules out except currency — the point
    is a record that IS current — and refuses a PR whose one re-sweep is
    already spent, so an unrunnable repro can never queue itself in a loop."""
    if _resweep_spent(pr):
        return False
    status = (pr.verify_request or {}).get("status")
    if status in ("queued", "running", "waiting-for-base", "error", "cancelled"):
        return False
    if not gates.security_cleared(pr):
        return False
    if not is_current(pr, "verify", max_age_days=gates.VERIFY_MAX_AGE_DAYS):
        return False
    if gates.repro_harness_defect(pr) is None:
        return False
    return gates.verify_eligible(pr, _changed_paths(pr))


def next_auto() -> tuple[str, int] | None:
    """The idle hunt's next pick, or None: ("security", n) while any clean
    merge candidate lacks a current security verdict, then ("verify", n) for
    the best GREEN-cleared unverified candidate, then ("resweep", n) for a
    concluded verification whose repro the harness broke. Every lane orders by
    highest community pain, then lowest PR number.

    Lane order is spend priority: a PR with no verification at all buys more
    than a second opinion on one that already concluded."""
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
    resweep_pool = [(n, pr) for n, pr in prs.items() if auto_resweepable(pr)]
    if resweep_pool:
        return ("resweep", min(resweep_pool, key=_key)[0])
    return None


def run_security(n: int) -> int:
    """Run the headless per-PR security review for PR `n` and return its exit
    code. A PR stays in security_failed until a run both exits 0 and leaves it
    outside the security pool: pipeline/security_review.py can exit 0 while
    holding an INCOMPLETE review (a lens agent failure) without writing a
    verdict, so the exit code alone does not prove a verdict landed. A fresh
    pre-spawn recheck skips a PR another
    process already cleared since it was picked (snapshot lag). The runner
    owns the verdict write, any RED flip, and the runs-ledger entry.

    The store claim is what keeps two idle machines off one PR: a review costs
    several agent runs, so losing the race means dropping the pick rather than
    racing to a duplicate verdict. The claim is released whatever the run does,
    so a failure leaves the PR free for the next attempt rather than parked."""
    st = data.store()
    rec = st.load_pr(n)
    if rec is None or not gates.blocked_on_security(rec):
        return 0
    me = socket.gethostname()
    if not st.claim_security_run(n, host=me, stale_after=SECURITY_CLAIM_SECONDS):
        print(f"[autohunt] PR #{n} is already under review elsewhere", flush=True)
        return 0
    try:
        return _run_security_claimed(n)
    finally:
        st.release_security_run(n, host=me)


def _run_security_claimed(n: int) -> int:
    """Spawn the security review for a PR this host holds the claim on."""
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


def auto_queue_verify(n: int, *, source: str = "auto") -> None:
    """Queue PR `n` for sandbox verification as an auto-pick. The drain loop
    runs it on its next pick exactly like an operator-queued request.
    `source` is RESWEEP_SOURCE on the repro re-sweep lane, which is what stops
    that lane picking the same PR twice."""
    print(f"[autohunt] queueing PR #{n} for verification ({source})", flush=True)
    verify_queue.queue_pr(n, source=source)


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
    """A pickup must end terminal or deliberately parked. A `running` leftover,
    or a `queued` one from a nonzero exit, means the orchestrator crashed
    before recording a result — mark it errored with the captured output, since
    a pending leftover would be picked up again next tick and crash-loop. A
    `queued` request from a clean exit is the orchestrator's own transient-
    failure re-queue and stays for the worker to re-pick. A request another
    host claimed is left alone: this pickup lost the claim race, and the
    running status is the winner's live run."""
    me = socket.gethostname()
    st = data.store()
    rec = st.load_pr(n)
    if rec is not None:
        req = rec.verify_request or {}
        status = req.get("status")
        crashed = status == "running" or (status == "queued" and rc != 0)
        if crashed and req.get("host") in (None, me):
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


def release_stale_claims() -> list[int]:
    """Drop this host's leftover security-review claims. A review does not
    survive the worker's process, so at startup a claim in this host's name is a
    restart's leftover; another host's is its live run. Returns what it freed."""
    me = socket.gethostname()
    st = data.store()
    freed: list[int] = []
    for n, rec in sorted(st.all_prs().items()):
        if (rec.raw.get("security_run") or {}).get("host") != me:
            continue
        st.release_security_run(n, host=me)
        freed.append(n)
    return freed


def _drain_loop() -> None:
    try:
        freed = release_stale_claims()
        if freed:
            print(f"[verify-worker] released security claims after restart: {freed}",
                  flush=True)
    except Exception:
        traceback.print_exc()
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
            elif lane == "resweep":
                auto_queue_verify(n, source=RESWEEP_SOURCE)
            else:
                auto_queue_verify(n)
        except Exception:
            traceback.print_exc()
            stop.wait(POLL_SECONDS)
        finally:
            state["current_pr"] = None


def startup() -> bool:
    """Start the heartbeat + drain threads when this backend is the runner.
    Returns whether the worker is running afterwards, so calling it on a live
    worker is a no-op rather than a second pair of loops."""
    if not enabled():
        return False
    if running():
        return True
    stop.clear()
    _threads[:] = [
        threading.Thread(target=_beat_loop, daemon=True, name="verify-worker-beat"),
        threading.Thread(target=_drain_loop, daemon=True, name="verify-worker"),
    ]
    for t in _threads:
        t.start()
    print(f"[verify-worker] enabled on {socket.gethostname()} "
          f"(poll every {POLL_SECONDS:.0f}s)", flush=True)
    return True
