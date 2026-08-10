"""The autofix worker: drains queued fix_requests by acting on contributor PR
head branches, one PR at a time, inside the app backend process.

It runs ONLY where TRIAGE_FIX_WORKER=1 is set — the machine holding the machine
user's SSH key. Every other app backend serves the same queue/approve API and
starts no worker; the queue lives in the shared store, so a click anywhere
reaches the runner here within one poll tick.

All git mechanics go through prospector_app/agent/resubmit, which owns the push
identity, the "Allow edits from maintainers" preflight, the pinned lease, and
the fence that refuses any ref that is not the open PR's head. The worker
decides *which* PR gets *which* action and whether the result may be pushed; it
never runs git itself.

`update` pushes on a clean local merge: it authors no content, and a merge that
conflicts stops before the push. `rebase` and `fix` additionally require a clean
compile preflight over the resulting tree, and park as `awaiting-review` with
the diff unless their action is named in TRIAGE_FIX_AUTOPUSH.

With TRIAGE_FIX_AUTOHUNT=1 an empty queue turns the drain loop into a hunter: it
queues the eligible PRs whose gates an autofix could plausibly clear, oldest
community pain first. An operator-queued request always wins the next pick.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import compile_preflight, gates, settings, verify_driver
from pipeline.storekit import now as _now
from prospector_app.backend import data, fix_queue, service
from prospector_app.backend.resubmit_identity import worker_env

if TYPE_CHECKING:
    from pipeline.model import Pr

POLL_SECONDS = 15.0

# Captured resubmit output kept on a request the worker has to mark failed.
TAIL_CHARS = 4000

# resubmit exits that describe a world that moved rather than a decision:
# a git/network failure (4), refs that shifted under the pin (6), and a push
# the remote rejected (7). Retrying re-reads the live PR and re-pins, which is
# exactly the remedy. Every other exit is a judgment — the PR is closed, the
# merge conflicts, the fence refused the ref — and repeating it changes nothing.
TRANSIENT_EXITS = {4, 6, 7}

# How many times a transient failure is re-queued before it is left for a human.
MAX_ATTEMPTS = 3

# How long a re-queued request rests before pickup, so a moving base or a
# flaking remote gets time to settle instead of burning the attempt cap in one
# minute.
TRANSIENT_RETRY_SECONDS = 300.0

REPO_ROOT = Path(__file__).resolve().parents[2]
RESUBMIT = REPO_ROOT / "prospector_app" / "agent" / "resubmit"

# The PR the drain loop is currently acting on (None when idle) — read by the
# heartbeat thread; `stop` ends both loops (tests set it).
state: dict[str, int | None] = {"current_pr": None}
stop = threading.Event()


def enabled() -> bool:
    """Whether THIS backend is the autofix runner. Deliberately an exact opt-in:
    only the machine whose .env sets TRIAGE_FIX_WORKER=1 drains the queue."""
    return settings.FIX_WORKER


def enabled_autohunt() -> bool:
    """Whether the idle auto-hunt runs on this backend. An exact opt-in like
    enabled(), and meaningful only alongside it."""
    return settings.FIX_AUTOHUNT


def key_safety_failure() -> str | None:
    """Why this machine must not run the worker, or None when it may.

    The push key is a passphrase-less credential that can write to contributor
    branches, and this is also the machine that runs untrusted contributor code
    in the verify sandbox. So the key must exist, be readable by its owner
    alone, and live outside the sandbox scratch root — properties asserted at
    startup rather than assumed."""
    if not settings.push_identity_configured():
        return ("no contributor-push identity is configured: set TRIAGE_PUSH_LOGIN, "
                "TRIAGE_PUSH_EMAIL and TRIAGE_PUSH_SSH_KEY_FILE in .env")
    key = settings.PUSH_SSH_KEY_FILE
    assert key is not None  # push_identity_configured() proved it
    try:
        mode = key.stat().st_mode
    except OSError as e:
        return f"TRIAGE_PUSH_SSH_KEY_FILE is unreadable ({key}): {e}"
    if not stat.S_ISREG(mode):
        return f"TRIAGE_PUSH_SSH_KEY_FILE is not a regular file: {key}"
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (f"TRIAGE_PUSH_SSH_KEY_FILE is group/world-accessible: {key} "
                f"(chmod 600 it)")
    scratch = settings.VERIFY_SCRATCH.resolve()
    resolved = key.resolve()
    if resolved == scratch or scratch in resolved.parents:
        return (f"TRIAGE_PUSH_SSH_KEY_FILE lives under the sandbox scratch root "
                f"({scratch}) — move the key outside anything the sandbox can reach")
    return None


def beat() -> None:
    """Write this worker's liveness and autohunt opt-in into the shared store."""
    data.store().save_fix_worker({
        "host": socket.gethostname(), "pid": os.getpid(),
        "last_beat": _now(), "current_pr": state["current_pr"],
        "autohunt": enabled_autohunt()})


def recover_orphans() -> list[int]:
    """Mark THIS host's `running`/`pushing` requests failed: an action does not
    survive the worker's process, so at startup such a status claimed by this
    host can only be a restart's leftover. A request another host claimed is its
    live run, never touched from here. Returns the PRs marked.

    A `pushing` orphan is reported as indeterminate: the push may or may not
    have gone out before the process died, so the operator re-reads the PR
    rather than the worker guessing."""
    marked: list[int] = []
    me = socket.gethostname()
    st = data.store()
    with st.batch():
        for n, rec in sorted(st.all_prs().items()):
            req = rec.fix_request or {}
            status = req.get("status")
            if status not in ("running", "pushing"):
                continue
            if req.get("host") not in (None, me):
                continue
            note = ("the autofix worker restarted mid-run — nothing was pushed; re-queue to retry"
                    if status == "running" else
                    "the autofix worker restarted mid-push — re-read the PR to see whether "
                    "the push landed before re-queueing")
            st.edit_pr(n).record_fix_request(
                "failed", req.get("action", "fix"), queued_at=req.get("queued_at"),
                started_at=req.get("started_at"), finished_at=_now(), error=note,
                source=req.get("source"), host=me)
            marked.append(n)
    return marked


def next_queued() -> int | None:
    """The best runnable PR, or None: every `queued` request, ranked operator
    picks before auto-picks and, within each group, oldest queued_at first — so
    an operator click never waits behind an earlier auto-queued request. Reads
    the backend's incremental store snapshot, so the scan costs no store
    round-trip."""
    return _oldest("queued")


def next_approved() -> int | None:
    """The oldest request an operator approved for pushing, or None."""
    return _oldest("approved")


def _oldest(status: str) -> int | None:
    best_n: int | None = None
    best_key: tuple[bool, str] | None = None
    for n, rec in data.prs().items():
        req = rec.fix_request or {}
        if req.get("status") != status:
            continue
        if req.get("attempts") and not _rested(req, TRANSIENT_RETRY_SECONDS):
            continue
        key = (req.get("source") == "auto", str(req.get("queued_at") or ""))
        if best_key is None or key < best_key:
            best_n, best_key = n, key
    return best_n


def _resubmit(pr: int, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the resubmit helper, which owns every git mechanic and the push
    identity. The worker opts into its machine user here; interactive invocations
    of the same helper retain the operator identity."""
    return subprocess.run([str(RESUBMIT), str(pr), *args], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=1800,
                          env=worker_env())


# What each resubmit exit means, in the words an operator would use. The raw
# stderr is kept alongside for anyone debugging, but it is never the headline:
# a stack trace or a docker argv tells an operator nothing about what to do.
_PLAIN_EXITS: dict[int, str] = {
    3: "This PR can't be pushed to — it's closed, or the author turned off "
       "\"Allow edits from maintainers\".",
    4: "A git command failed talking to GitHub. Usually a network blip; it retries.",
    5: "There was nothing to push — no change was produced.",
    6: "The author pushed to this branch while we were working, so the change no "
       "longer applies. It retries against their new commit.",
    7: "GitHub rejected the push. Usually the branch moved underneath us; it retries.",
    8: "The base branch conflicts with this PR, so it can't be merged in "
       "automatically. Try \"Resolve merge conflicts\", or ask the author to update.",
    9: "The rebase couldn't be completed automatically.",
    11: "The rebase confirmation didn't match the author's current commit.",
    12: "Refused: the push would have targeted a branch other than this PR's own.",
}


def plain_reason(rc: int, output: str) -> str:
    """A one-line explanation of a failed run for the operator, with the raw
    output kept out of it. An unmapped exit falls back to the first line of what
    the command actually said, which is at least a sentence rather than a
    traceback."""
    mapped = _PLAIN_EXITS.get(rc)
    if mapped:
        return mapped
    first = next((ln.strip() for ln in output.splitlines() if ln.strip()), "")
    first = first.removeprefix("resubmit: ")
    return first or f"The {rc and 'action failed' or 'action failed'} (exit {rc})."


def plain_preflight(pf: dict) -> str:
    """Why the compile preflight did not clear the change, in plain words.

    A refusal is a policy decision and already reads as a sentence. An `error` is
    an exception string from the sandbox — docker argv, a Python class name —
    which says nothing an operator can act on beyond "the sandbox is broken on
    this machine"."""
    if pf.get("refused"):
        return f"The change wasn't compile-checked: {pf['refused']}"
    if pf.get("error"):
        return ("The build check couldn't run — the sandbox failed to start on the "
                "worker machine. Nothing was pushed. This is a problem with the "
                "worker, not with the PR.")
    if pf.get("exit") not in (0, None):
        return "The project didn't compile with this change applied, so nothing was pushed."
    return "The build check did not pass."


def _rested(req: dict, seconds: float) -> bool:
    """Whether a re-queued request's last attempt (its checked_at write stamp) is
    at least `seconds` old. A missing or unparseable stamp reads as rested, so a
    malformed record cannot wait forever."""
    stamp = req.get("checked_at")
    if not isinstance(stamp, str):
        return True
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - at).total_seconds() >= seconds


def _settle(n: int, req: dict, rc: int, output: str) -> None:
    """Record a failed resubmit run: re-queue it when the exit says the world
    moved and attempts remain, otherwise refuse it for a human to look at."""
    attempts = int(req.get("attempts") or 0) + 1
    if rc in TRANSIENT_EXITS and attempts < MAX_ATTEMPTS:
        data.store().edit_pr(n).record_fix_request(
            "queued", req.get("action", "fix"), queued_at=req.get("queued_at"),
            attempts=attempts, source=req.get("source"), host=socket.gethostname(),
            error=f"attempt {attempts} did not stick, retrying: {output[-TAIL_CHARS:]}")
        data.refresh()
        print(f"[fix-worker] PR #{n} exited {rc}; re-queued (attempt {attempts}"
              f"/{MAX_ATTEMPTS})", flush=True)
        return
    reason = plain_reason(rc, output)
    if rc in TRANSIENT_EXITS:
        reason = f"{reason} Gave up after {attempts} attempts."
    _refuse(n, req, reason, result={"output": output[-TAIL_CHARS:]})


def _fail(n: int, req: dict, message: str) -> None:
    data.store().edit_pr(n).record_fix_request(
        "failed", req.get("action", "fix"), queued_at=req.get("queued_at"),
        started_at=req.get("started_at"), finished_at=_now(),
        error=message[-TAIL_CHARS:], source=req.get("source"),
        host=socket.gethostname())
    data.refresh()


def _refuse(n: int, req: dict, reason: str, result: dict | None = None) -> None:
    data.store().edit_pr(n).record_fix_request(
        "refused", req.get("action", "fix"), queued_at=req.get("queued_at"),
        started_at=req.get("started_at"), finished_at=_now(),
        refused_reason=reason[-TAIL_CHARS:], result=result,
        source=req.get("source"), host=socket.gethostname())
    data.refresh()


def recheck_eligibility(n: int, action: str) -> tuple[bool, str]:
    """Re-run the autofix gate against the record as it stands now. A request
    can sit in the queue while a threat scan or security review lands, so the
    gate that allowed the queue click is re-asked before the worker acts."""
    rec = data.store().load_pr(n)
    if rec is None:
        return False, f"PR #{n} left the store"
    return gates.fix_eligibility(rec, action, service.changed_paths(rec))


def run_one(n: int) -> None:
    """Act on one claimed PR. Every exit writes a terminal status, so a request
    never sits `running` after this returns."""
    host = socket.gethostname()
    claimed = data.store().claim_fix_request(n, host=host)
    if claimed is None:
        return  # another worker got there first, or the request moved on
    action = claimed.get("action") or "fix"
    ok, why = recheck_eligibility(n, action)
    if not ok:
        _refuse(n, claimed, f"no longer eligible for {action}: {why}")
        return

    try:
        patch = _probe(n, claimed, action)
        if patch is None:
            return  # _probe wrote the terminal status
        pf = _preflight(n, patch)
        if pf is not None:
            pf_ok, pf_why = gates.compile_preflight_gate(pf)
            if not pf_ok:
                _resubmit(n, "abort")
                _refuse(n, claimed, plain_preflight(pf),
                        result={"patch": patch[-TAIL_CHARS:], "compile_preflight": pf,
                                "detail": pf_why})
                return
        result = {"patch": patch[-TAIL_CHARS:], "compile_preflight": pf,
                  "message": _commit_message(action)}
        if action not in settings.FIX_AUTOPUSH:
            _park(n, claimed, action, result, host)
            return
        _push(n, claimed, action, result)
    except Exception:
        _resubmit(n, "abort")
        raise


def _probe(n: int, claimed: dict, action: str) -> str | None:
    """The change `action` would push, without pushing it, or None when the probe
    ended the request instead. The patch is the PR's whole change relative to
    current base, which is what compile_preflight.run_for_patch applies over
    default-branch HEAD — so the tree it measures is the tree a push would
    produce."""
    if action == "update":
        # A base merge is one atomic resubmit command, so its probe is the same
        # command stopped before the push rather than a prepare/diff pair.
        r = _resubmit(n, "update", "--probe")
        if r.returncode != 0:
            _settle(n, claimed, r.returncode, (r.stderr or r.stdout).strip())
            return None
        patch = (r.stdout or "").strip()
    else:
        prepared = _resubmit(n, "prepare", *(["--rebase"] if action == "rebase" else []))
        if prepared.returncode != 0:
            _settle(n, claimed, prepared.returncode,
                    (prepared.stderr or prepared.stdout).strip())
            return None
        # `prepare --rebase` exits 0 both when the rebase finished and when it
        # PAUSED on conflicts git could not resolve. A paused rebase leaves
        # conflict markers in the tree, so anything read from it is not a change
        # — it is an unfinished merge. Nothing downstream may treat it as one.
        paused = _conflicted_state(n)
        if paused is not None:
            _resubmit(n, "abort")
            files = ", ".join(paused[:5])
            more = f" (and {len(paused) - 5} more)" if len(paused) > 5 else ""
            _refuse(n, claimed,
                    f"This PR's changes and the current base both edit the same lines "
                    f"in {len(paused)} file(s), and git can't combine them on its own: "
                    f"{files}{more}. Resolving that needs a person who knows which "
                    f"version is right — ask the author to rebase.")
            return None
        diff = _resubmit(n, "diff")
        if diff.returncode != 0:
            _fail(n, claimed, (diff.stderr or diff.stdout).strip())
            return None
        patch = (diff.stdout or "").strip()
    if not patch:
        _resubmit(n, "abort")
        _refuse(n, claimed, f"the {action} produced no change to push")
        return None
    return patch


def _park(n: int, claimed: dict, action: str, result: dict, host: str) -> None:
    """Record a proven change for an operator to approve, pushing nothing.

    A mechanical action's worktree is discarded here: push_approved re-derives it
    against whatever base is current then, so holding one would only accumulate
    clones on the sandbox machine while a browsable backlog sits unreviewed. An
    agent-authored `fix` is not reproducible, so its tree stays."""
    if action in gates.HUNTABLE_ACTIONS:
        _resubmit(n, "abort")
    data.store().edit_pr(n).record_fix_request(
        "awaiting-review", action, queued_at=claimed.get("queued_at"),
        started_at=claimed.get("started_at"), result=result,
        source=claimed.get("source"), host=host, base_sha=_base_sha(),
        head_sha=claimed.get("against_head_sha"))
    data.refresh()


def _base_sha() -> str | None:
    """Upstream's default-branch HEAD, or None when it cannot be read. It stamps
    which base a parked result was proven against, so the app can say when that
    proof has aged. Advisory only — the approve path re-proves regardless, so a
    failure here must not cost the operator the parked result."""
    try:
        return verify_driver.resolve_base_sha()
    except Exception:
        return None


def _conflicted_state(n: int) -> list[str] | None:
    """The conflicted paths when the prepared rebase is paused on them, else
    None. Reads `resubmit state` rather than the exit code, which cannot tell a
    resumable pause from a finished rebase."""
    r = _resubmit(n, "state")
    if r.returncode != 0:
        return None
    try:
        state = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    conflicts = state.get("conflicts") or []
    if state.get("phase") == "conflicted" or conflicts:
        return [str(c) for c in conflicts] or ["(unnamed)"]
    return None


def _preflight(n: int, patch: str) -> dict | None:
    """The compile-preflight record for the authored tree, or None when the
    profile configures no compile command. The patch is written where the
    sandbox driver reads it from, so the change is measured before it ever
    reaches the contributor's branch."""
    rec = data.store().load_pr(n)
    head = (rec.head_sha if rec else "") or ""
    scratch = settings.VERIFY_SCRATCH / "autofix"
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / f"pr-{n}.patch"
    path.write_text(patch + "\n")
    return compile_preflight.run_for_patch(n, head, path)


def _commit_message(action: str) -> str:
    return ("Rebase onto current base" if action == "rebase"
            else "Address review and CI feedback")


def push_approved(n: int) -> None:
    """Push a request an operator approved, re-deriving a mechanical change
    against current base first.

    The stored result proves the change applied to the base it was measured
    against, which an active repository moves past continuously. Re-running the
    merge or rebase is what makes the approval mean "push this against main as it
    stands now" rather than "replay a verdict from Tuesday" — and it fails loudly,
    as a refusal naming the conflicting paths, when that is no longer possible."""
    host = socket.gethostname()
    claimed = data.store().claim_fix_request(n, host=host, statuses=("approved",),
                                             to_status="pushing")
    if claimed is None:
        return
    action = claimed.get("action") or "fix"
    ok, why = recheck_eligibility(n, action)
    if not ok:
        _resubmit(n, "abort")
        _refuse(n, claimed, f"no longer eligible for {action}: {why}")
        return
    if action == "rebase":
        # `update` re-derives inside its own push command; a rebase needs the
        # worktree back before push can rewrite anything.
        prepared = _resubmit(n, "prepare", "--rebase")
        if prepared.returncode != 0:
            _settle(n, claimed, prepared.returncode,
                    (prepared.stderr or prepared.stdout).strip())
            return
        paused = _conflicted_state(n)
        if paused is not None:
            _resubmit(n, "abort")
            _refuse(n, claimed,
                    f"The base moved since this rebase was proven, and it now "
                    f"conflicts on {len(paused)} file(s): {', '.join(paused[:5])}. "
                    f"Nothing was pushed.")
            return
    _push(n, claimed, action, claimed.get("result") or {})


def _push(n: int, req: dict, action: str, result: dict) -> None:
    # `update` merges against current base and pushes in one command, so it is
    # its own re-derivation; the other actions push a tree already prepared.
    args = (["update"] if action == "update" else
            ["push", "--confirm-rewrite", str(req.get("against_head_sha") or "")]
            if action == "rebase" else
            ["push", "-m", str(result.get("message") or _commit_message(action))])
    r = _resubmit(n, *args)
    if r.returncode != 0:
        _resubmit(n, "abort")
        _settle(n, req, r.returncode, (r.stderr or r.stdout).strip())
        return
    _finish_pushed(n, req, (r.stdout or "").strip(), result=result)


def _finish_pushed(n: int, req: dict, output: str, result: dict | None = None) -> None:
    merged = dict(result or {})
    merged["output"] = output[-TAIL_CHARS:]
    data.store().edit_pr(n).record_fix_request(
        "pushed", req.get("action", "fix"), queued_at=req.get("queued_at"),
        started_at=req.get("started_at"), finished_at=_now(), result=merged,
        source=req.get("source"), host=socket.gethostname())
    data.refresh()


def auto_fixable(pr: Pr) -> str | None:
    """The action the idle hunter would queue for this PR, or None. Only the
    mechanical actions are hunted: an agent-authored fix is an operator's call,
    never something the queue starts on its own.

    A PR GitHub reports unmergeable needs its history replayed on current base,
    which is `rebase`; a PR whose drift scan says the base moved out from under
    it needs `update`. Anything else is left alone.

    gates.fix_huntable is the bar, not fix_eligibility: unprompted sandbox time
    goes to PRs a reviewer already rated, so a branch nobody has vouched for is
    left for an operator to queue by hand."""
    if (pr.fix_request or {}).get("status") in fix_queue.IN_FLIGHT:
        return None
    if pr.mergeable is False:
        action = "rebase"
    elif pr.drift_state == "conflicts":
        action = "update"
    else:
        return None
    ok, _ = gates.fix_huntable(pr, action, service.changed_paths(pr))
    return action if ok else None


def next_auto() -> tuple[str, int] | None:
    """The idle hunter's next (action, PR), or None when nothing is eligible."""
    for n, rec in sorted(data.prs().items()):
        action = auto_fixable(rec)
        if action is not None:
            return action, n
    return None


def _beat_loop() -> None:
    while not stop.is_set():
        try:
            beat()
        except Exception:
            traceback.print_exc()
        stop.wait(POLL_SECONDS)


def _drain_loop() -> None:
    try:
        marked = recover_orphans()
        if marked:
            print(f"[fix-worker] marked failed after restart: {marked}", flush=True)
    except Exception:
        traceback.print_exc()
    while not stop.is_set():
        try:
            n = next_approved()
            if n is not None:
                state["current_pr"] = n
                beat()
                print(f"[fix-worker] pushing approved fix for PR #{n}", flush=True)
                push_approved(n)
                continue
            n = next_queued()
            if n is not None:
                state["current_pr"] = n
                beat()
                print(f"[fix-worker] picking up PR #{n}", flush=True)
                run_one(n)
                continue
            pick = next_auto() if enabled_autohunt() else None
            if pick is None:
                stop.wait(POLL_SECONDS)
                continue
            action, n = pick
            print(f"[autofix-hunt] queueing {action} for PR #{n}", flush=True)
            fix_queue.queue_pr(n, action, source="auto")
        except Exception:
            traceback.print_exc()
            stop.wait(POLL_SECONDS)
        finally:
            state["current_pr"] = None


def startup() -> bool:
    """Start the heartbeat + drain threads when this backend is the runner.
    Returns whether the worker started. A machine that opts in but cannot hold
    the push credential safely refuses to start and says why — the queue API
    keeps serving, so the refusal never takes the app down with it."""
    if not enabled():
        return False
    failure = key_safety_failure()
    if failure is not None:
        print(f"[fix-worker] NOT started: {failure}", flush=True)
        return False
    threading.Thread(target=_beat_loop, daemon=True, name="fix-worker-beat").start()
    threading.Thread(target=_drain_loop, daemon=True, name="fix-worker").start()
    print(f"[fix-worker] enabled on {socket.gethostname()} as {settings.PUSH_LOGIN} "
          f"(poll every {POLL_SECONDS:.0f}s)", flush=True)
    return True
