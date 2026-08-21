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

Every action is probed before it is pushed: the mechanics run in full, the
resulting tree goes through the compile preflight, and the request parks as
`awaiting-review` with its evidence unless TRIAGE_FIX_AUTOPUSH names the action.
So the hunter can be left on against a repository it never touches.

A mechanical rebase that pauses on real conflicts escalates, for
operator-clicked requests only, to an agent-authored merge resolution that
parks as a `resolve` request for review.

A parked mechanical request keeps no worktree — push_approved re-derives it
against whatever base is current then, which is what stops a browsable backlog
from accumulating clones or pushing a result proven against a base that has
since moved. An agent-authored `fix` or `resolve` is not reproducible, so it
keeps its tree and pushes the reviewed change verbatim.

With TRIAGE_FIX_AUTOHUNT=1 an empty queue turns the drain loop into a hunter: it
queues the eligible PRs whose gates an autofix could plausibly clear — every
action one unattended attempt per head, so a refused run rests until the
author pushes. With TRIAGE_FIX_HUNT_FIX=1 on top, the hunter also queues
agent-authored `fix` actions — at most TRIAGE_FIX_HUNT_LIMIT in flight — for
mergeable, CI-passing PRs scored below the review bar. An operator-queued
request always wins the next pick.
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

from pipeline import (author_fix, compile_preflight, diffpaths, gates, gh, profile,
                      resolve_conflicts, review_fix, settings, verify_driver)
from pipeline.storekit import now as _now
from prospector_app.backend import data, executor, fix_queue, review_refresh, service
from prospector_app.backend.resubmit_identity import worker_env

if TYPE_CHECKING:
    from pipeline.model import Pr

POLL_SECONDS = 15.0

# Largest authored patch the queue will carry. The approve path re-applies these
# exact bytes on whichever machine pushes, so a patch is stored whole or not at
# all — a truncated one would neither apply nor show the operator what they are
# approving. A change this large is past what an unattended agent should be
# writing, so exceeding the cap refuses the request rather than trimming it.
PATCH_CHARS = 200_000

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
    """Whether THIS backend is the autofix runner. Deliberately an exact opt-in:
    a machine says so in its own .env."""
    return settings.fix_worker_enabled()


def enabled_autohunt() -> bool:
    """Whether the idle auto-hunt runs on this backend. An exact opt-in like
    enabled(), and meaningful only alongside it."""
    return settings.fix_autohunt()


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
    scratch = settings.verify_scratch().resolve()
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
    """The oldest request an operator approved for pushing, or None.

    A `resolve` is skipped unless this machine authored it: the merge commit
    being approved lives in that machine's worktree, and no other can rebuild
    it. Every other action re-derives from the store, so any machine may push
    one."""
    return _oldest("approved", mine_only=("resolve",))


def _oldest(status: str, mine_only: tuple[str, ...] = ()) -> int | None:
    """The best PR at `status`, operator picks before auto-picks and oldest
    first within each. An action named in `mine_only` is passed over unless this
    machine recorded it (or the record names none, from before hosts were
    stamped) — it depends on state only that machine holds."""
    me = socket.gethostname()
    best_n: int | None = None
    best_key: tuple[bool, str] | None = None
    for n, rec in data.prs().items():
        req = rec.fix_request or {}
        if req.get("status") != status:
            continue
        if req.get("action") in mine_only and req.get("host") not in (None, me):
            continue
        if req.get("attempts") and not _rested(req, TRANSIENT_RETRY_SECONDS):
            continue
        key = (req.get("source") == "auto", str(req.get("queued_at") or ""))
        if best_key is None or key < best_key:
            best_n, best_key = n, key
    return best_n


def _resubmit(pr: int, *args: str,
              stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the resubmit helper, which owns every git mechanic and the push
    identity. The worker opts into its machine user here; interactive invocations
    of the same helper retain the operator identity. `stdin` feeds a patch to
    `apply` down the pipe, so the reviewed bytes never land on disk."""
    return subprocess.run([str(RESUBMIT), str(pr), *args], cwd=str(REPO_ROOT),
                          input=stdin, capture_output=True, text=True, timeout=1800,
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
            guidance=req.get("guidance"),
            error=f"attempt {attempts} did not stick, retrying: {output[-TAIL_CHARS:]}")
        data.refresh()
        print(f"[fix-worker] PR #{n} exited {rc}; re-queued (attempt {attempts}"
              f"/{MAX_ATTEMPTS})", flush=True)
        return
    reason = plain_reason(rc, output)
    if rc in TRANSIENT_EXITS:
        reason = f"{reason} Gave up after {attempts} attempts."
    _refuse(n, req, reason, result={"output": output[-TAIL_CHARS:]})


def _log_run(n: int, req: dict, status: str, detail: str | None = None,
             host: str | None = None) -> None:
    """Append this run's ending to the runs ledger. A PR carries one
    fix_request, which the next queue click overwrites — the ledger is where an
    action's outcome survives that, and what the app's fix history reads.

    Best-effort: a ledger append that fails must not cost the operator the
    terminal status the caller has already written."""
    entry = {
        "phase": "fix:single", "pr": n, "started": req.get("started_at"),
        "finished": _now(),
        "trigger": "autohunt" if req.get("source") == "auto" else None,
        "stats": {"status": status, "action": req.get("action", "fix"),
                  "detail": detail, "host": host or socket.gethostname()}}
    try:
        data.store().append_run(entry)
    except Exception:
        traceback.print_exc()


def _fail(n: int, req: dict, message: str) -> None:
    data.store().edit_pr(n).record_fix_request(
        "failed", req.get("action", "fix"), queued_at=req.get("queued_at"),
        started_at=req.get("started_at"), finished_at=_now(),
        error=message[-TAIL_CHARS:], source=req.get("source"),
        guidance=req.get("guidance"), host=socket.gethostname(),
        head_sha=req.get("against_head_sha"))
    data.refresh()
    _log_run(n, req, "failed", message[-TAIL_CHARS:])


def _refuse(n: int, req: dict, reason: str, result: dict | None = None) -> None:
    data.store().edit_pr(n).record_fix_request(
        "refused", req.get("action", "fix"), queued_at=req.get("queued_at"),
        started_at=req.get("started_at"), finished_at=_now(),
        refused_reason=reason[-TAIL_CHARS:], result=result,
        source=req.get("source"), guidance=req.get("guidance"),
        host=socket.gethostname(), head_sha=req.get("against_head_sha"))
    data.refresh()
    _log_run(n, req, "refused", reason[-TAIL_CHARS:])


def recheck_eligibility(n: int, action: str,
                        paths: list[str] | None = None) -> tuple[bool, str]:
    """Re-run the autofix gate against the record as it stands now. A request
    can sit in the queue while a threat scan or security review lands, so the
    gate that allowed the queue click is re-asked before the worker acts.

    `paths` overrides the PR's own changed paths with the content the agent
    authored — a `resolve`'s conflicted paths, a `fix`'s finished patch — which
    is the set that has to clear the gate for those actions. The stored
    guidance carries the operator's mandate, so this asks exactly what the
    queue click asked."""
    rec = data.store().load_pr(n)
    if rec is None:
        return False, f"PR #{n} left the store"
    guidance = (rec.fix_request or {}).get("guidance")
    return gates.fix_eligibility(rec, action,
                                 paths if paths is not None else service.changed_paths(rec),
                                 guided=bool(guidance))


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
        if action == "fix":
            _author_fix(n, claimed)
            return
        patch = _probe(n, claimed, action)
        if patch is None:
            return  # _probe wrote the terminal status
        _running_step(n, claimed, "compile preflight", action=action)
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
        if action not in settings.fix_autopush():
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
        _running_step(n, claimed, "merging base in", action=action)
        r = _resubmit(n, "update", "--probe")
        if r.returncode != 0:
            _settle(n, claimed, r.returncode, (r.stderr or r.stdout).strip())
            return None
        patch = (r.stdout or "").strip()
    else:
        _running_step(n, claimed, "rebasing onto base", action=action)
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
            _agent_resolve(n, claimed, paused)
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


DEFAULT_FIX_GOAL = ("Clear the failing gates on this pull request that a change "
                    "to its code can clear.")


def _fix_goal(rec: Pr, claimed: dict) -> tuple[str, list[dict], list[str]]:
    """What the authoring agent is being asked to do, and the evidence it works
    from: (goal, review findings, failing check names).

    Operator guidance is the goal when it is present. Otherwise the goal is the
    profile's fixable gates, described from the outstanding findings and the
    failing checks — the two things the store can say are wrong with the code.

    Findings are supplied only while the review verdict is current. A stale
    verdict describes a head the author has moved past, so it would send the
    agent after defects that may already be fixed."""
    findings: list[dict] = []
    section = rec.greptile_review or {}
    if section and not rec.review_stale:
        findings = [f for f in (section.get("findings") or []) if isinstance(f, dict)]
    fixable = profile.active().autofix.fixable_gates
    checks: list[str] = []
    if rec.ci == "failing" and ("ci" in fixable or claimed.get("guidance")):
        checks = [str(c.get("name")) for c in gh.check_runs(rec.head_sha or "")
                  if c.get("conclusion") == "failure" and c.get("name")]
    guidance = claimed.get("guidance")
    if guidance:
        return str(guidance), findings, checks
    goals = []
    if "review" in fixable and findings:
        goals.append("Fix the outstanding review findings listed below.")
    if "ci" in fixable and checks:
        goals.append("Make the failing CI checks listed below pass.")
    return ("\n".join(goals) or DEFAULT_FIX_GOAL), findings, checks


def _prepared_worktree(n: int) -> str | None:
    """The path of the worktree `prepare` just cloned, or None when the state
    cannot be read."""
    r = _resubmit(n, "state")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "{}").get("worktree")
    except ValueError:
        return None


def _author_fix(n: int, claimed: dict) -> None:
    """Author a change against this PR's gates with an agent, review it with a
    second one, and park the result for an operator to approve — or push it
    directly when TRIAGE_FIX_AUTOPUSH names `fix`.

    The agent writes inside a clone of the contributor's branch, the finished
    patch is held to the files the agent reported, re-gated on the paths it
    really touched, refuted by a reviewer that did not write it, and compiled —
    and only then does it park or push. Every exit writes a terminal status;
    the worktree survives only on the parked path, because an agent's edits
    cannot be re-derived at approval time."""
    rec = data.store().load_pr(n)
    if rec is None:
        _refuse(n, claimed, f"PR #{n} left the store")
        return
    goal, findings, checks = _fix_goal(rec, claimed)

    prepared = _resubmit(n, "prepare")
    if prepared.returncode != 0:
        _settle(n, claimed, prepared.returncode,
                (prepared.stderr or prepared.stdout).strip())
        return
    worktree = _prepared_worktree(n)
    if not worktree:
        _resubmit(n, "abort")
        _fail(n, claimed, "the prepared worktree could not be read")
        return

    _running_step(n, claimed, "agent authoring the fix", action="fix")
    try:
        verdict = author_fix.author(worktree, pr=n, title=rec.title or "",
                                    body=rec.body or "", goal=goal,
                                    findings=findings, ci_failures=checks)
    except (RuntimeError, ValueError) as e:
        _resubmit(n, "abort")
        _refuse(n, claimed, f"The agent attempt did not land: {e}")
        return
    if "give_up" in verdict:
        _resubmit(n, "abort")
        _refuse(n, claimed, f"The agent declined to write a change: "
                            f"{verdict['give_up']}")
        return

    diff = _resubmit(n, "diff")
    if diff.returncode != 0:
        _resubmit(n, "abort")
        _fail(n, claimed, f"reading the authored diff failed: "
                          f"{(diff.stderr or diff.stdout).strip()[:500]}")
        return
    patch = (diff.stdout or "").strip()
    if not patch.startswith("diff "):
        _resubmit(n, "abort")
        _refuse(n, claimed, "The agent reported changes, but the worktree holds "
                            "none — nothing was written.")
        return
    if len(patch) > PATCH_CHARS:
        _resubmit(n, "abort")
        _refuse(n, claimed, f"The agent wrote {len(patch)} characters of diff, past "
                            f"the {PATCH_CHARS} the queue carries. A change this "
                            f"large belongs to a person, not an unattended fix.")
        return
    if "\nBinary files " in patch or patch.startswith("Binary files "):
        # A textual diff names a binary change without carrying it, so the
        # reviewed bytes could not be re-applied at approval time.
        _resubmit(n, "abort")
        _refuse(n, claimed, "The agent changed a binary file, which the reviewed "
                            "patch cannot carry.")
        return
    paths = diffpaths.changed_paths(patch)
    try:
        author_fix.assert_disclosed(verdict["changes"], paths)
    except ValueError as e:
        _resubmit(n, "abort")
        _refuse(n, claimed, f"The authored change was not trusted: {e}",
                result={"patch": patch})
        return
    ok, why = recheck_eligibility(n, "fix", paths)
    if not ok:
        _resubmit(n, "abort")
        _refuse(n, claimed, f"The change the agent wrote is not one the bot may "
                            f"push: {why}", result={"patch": patch})
        return

    _running_step(n, claimed, "reviewing the authored change", action="fix")
    review = review_fix.review(worktree, patch, pr=n, goal=goal, findings=findings)
    evidence = {"patch": patch, "changes": verdict["changes"],
                "review_verdict": review}
    if review["verdict"] != "safe":
        _resubmit(n, "abort")
        _refuse(n, claimed, f"The reviewing agent rejected the change: "
                            f"{review['reason']}", result=evidence)
        return

    _running_step(n, claimed, "compile preflight", action="fix")
    pf = _preflight(n, patch)
    if pf is not None:
        pf_ok, pf_why = gates.compile_preflight_gate(pf)
        if not pf_ok:
            _resubmit(n, "abort")
            _refuse(n, claimed, plain_preflight(pf),
                    result={**evidence, "compile_preflight": pf, "detail": pf_why})
            return

    result = {**evidence, "compile_preflight": pf,
              "message": verdict["summary"] or _commit_message("fix")}
    if "fix" not in settings.fix_autopush():
        _park(n, claimed, "fix", result, socket.gethostname())
        return
    _push(n, claimed, "fix", result)


def _conflict_refusal(paused: list[str]) -> str:
    files = ", ".join(paused[:5])
    more = f" (and {len(paused) - 5} more)" if len(paused) > 5 else ""
    return (f"This PR's changes and the current base both edit the same lines "
            f"in {len(paused)} file(s), and git can't combine them on its own: "
            f"{files}{more}. Resolving that needs a person who knows which "
            f"version is right — ask the author to rebase.")


def _running_step(n: int, claimed: dict, step: str, action: str = "resolve") -> None:
    data.store().edit_pr(n).record_fix_request(
        "running", action, queued_at=claimed.get("queued_at"),
        started_at=claimed.get("started_at"), step=step,
        source=claimed.get("source"), guidance=claimed.get("guidance"),
        host=socket.gethostname(), head_sha=claimed.get("against_head_sha"))
    data.refresh()


def _agent_resolve(n: int, claimed: dict, paused: list[str]) -> None:
    """Escalate a rebase that paused on conflicts to an agent-authored merge
    resolution, parking the result as a `resolve` request for operator review.

    Only operator-clicked requests escalate — the hunter's picks refuse, so
    unattended agent time is never spent without a human having asked. Every
    exit path writes a terminal status and leaves no paused git state behind;
    the one worktree that survives is the parked resolution's, kept because an
    agent's edits are not mechanically re-derivable."""
    merge_diff = _conflict_diff(n)
    _resubmit(n, "abort")
    evidence = ({"merge_diff": merge_diff, "conflict_paths": paused}
                if merge_diff else None)
    if claimed.get("source") == "auto":
        _refuse(n, claimed, _conflict_refusal(paused), result=evidence)
        return
    rec = data.store().load_pr(n)
    if rec is None:
        _refuse(n, claimed, f"PR #{n} left the store")
        return
    ok, why = gates.fix_eligibility(rec, "resolve", paused)
    if not ok:
        _refuse(n, claimed,
                f"{_conflict_refusal(paused)} An agent resolution was withheld: {why}.",
                result=evidence)
        return

    prepared = _resubmit(n, "prepare", "--merge")
    if prepared.returncode != 0:
        _settle(n, claimed, prepared.returncode,
                (prepared.stderr or prepared.stdout).strip())
        return
    state_r = _resubmit(n, "state")
    try:
        st = json.loads(state_r.stdout or "{}")
    except ValueError:
        st = {}
    worktree = st.get("worktree")
    conflicts = [str(c) for c in (st.get("conflicts") or [])]
    if not worktree or not conflicts:
        # The merge did not pause where the rebase did; nothing to resolve here.
        _resubmit(n, "abort")
        _refuse(n, claimed, _conflict_refusal(paused), result=evidence)
        return

    _running_step(n, claimed, "agent resolving conflicts")
    try:
        verdict = resolve_conflicts.resolve(
            worktree, conflicts, pr=n, title=rec.title or "",
            body=rec.body or "", base_branch=str(st.get("base_branch") or ""))
    except (RuntimeError, ValueError) as e:
        _resubmit(n, "abort")
        _refuse(n, claimed,
                f"{_conflict_refusal(paused)} The agent attempt did not land: {e}.",
                result=evidence)
        return
    if "give_up" in verdict:
        _resubmit(n, "abort")
        _refuse(n, claimed,
                f"{_conflict_refusal(paused)} The agent declined to guess: "
                f"{verdict['give_up']}",
                result=evidence)
        return

    cont = _resubmit(n, "continue")
    if cont.returncode != 0:
        _resubmit(n, "abort")
        _refuse(n, claimed,
                f"{_conflict_refusal(paused)} The agent's resolution did not pass the "
                f"merge checks: {(cont.stderr or cont.stdout).strip()[:500]}",
                result=evidence)
        return
    diff = _resubmit(n, "diff")
    if diff.returncode != 0 or not (diff.stdout or "").strip():
        _resubmit(n, "abort")
        _fail(n, claimed, f"reading the resolved diff failed: "
                          f"{(diff.stderr or diff.stdout).strip()[:500]}")
        return
    patch = diff.stdout.strip()

    _running_step(n, claimed, "compile preflight")
    pf = _preflight(n, patch)
    if pf is not None:
        pf_ok, pf_why = gates.compile_preflight_gate(pf)
        if not pf_ok:
            _resubmit(n, "abort")
            _refuse(n, claimed, plain_preflight(pf),
                    result={"patch": patch[-TAIL_CHARS:], "compile_preflight": pf,
                            "detail": pf_why, "merge_diff": merge_diff,
                            "conflict_paths": paused})
            return

    result = {"patch": patch[-TAIL_CHARS:], "compile_preflight": pf,
              "resolutions": verdict["resolutions"], "conflict_paths": paused,
              "merge_diff": merge_diff,
              "message": "Merge current base, conflicts agent-resolved"}
    _park(n, claimed, "resolve", result, socket.gethostname())


def _park(n: int, claimed: dict, action: str, result: dict, host: str) -> None:
    """Record a proven change for an operator to approve, pushing nothing.

    The worktree is discarded for every action push_approved can rebuild: a
    mechanical one re-derives against whatever base is current then, and a `fix`
    re-applies its reviewed patch to a fresh clone. Holding a clone would only
    accumulate them on one machine while a browsable backlog sits unreviewed —
    and would tie the approval to the machine that authored it. A `resolve` keeps
    its tree, because the merge commit it holds is the thing being approved."""
    if action != "resolve":
        _resubmit(n, "abort")
    data.store().edit_pr(n).record_fix_request(
        "awaiting-review", action, queued_at=claimed.get("queued_at"),
        started_at=claimed.get("started_at"), result=result,
        source=claimed.get("source"), guidance=claimed.get("guidance"),
        host=host, base_sha=_base_sha(),
        head_sha=claimed.get("against_head_sha"))
    data.refresh()
    _log_run(n, {**claimed, "action": action}, "awaiting-review",
             result.get("message"), host=host)


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


# A conflict diff is kept whole up to this cap so the app can render every
# conflicted hunk; the cap keeps a pathological merge from bloating the store.
MERGE_DIFF_CHARS = 200_000


def _conflict_diff(n: int) -> str | None:
    """The paused rebase's conflict diff (`resubmit diff` over the paused
    worktree), or None when no diff can be read. Captured before the abort
    discards the worktree; the app's diff panel shows it beside the refusal.
    Output that is not a git diff — resubmit's own status chatter — is
    discarded rather than stored as one."""
    r = _resubmit(n, "diff")
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out.startswith("diff "):
        return None
    return out[:MERGE_DIFF_CHARS]


def _preflight(n: int, patch: str) -> dict | None:
    """The compile-preflight record for the authored tree, or None when the
    profile configures no compile command. The patch is written where the
    sandbox driver reads it from, so the change is measured before it ever
    reaches the contributor's branch."""
    rec = data.store().load_pr(n)
    head = (rec.head_sha if rec else "") or ""
    scratch = settings.verify_scratch() / "autofix"
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / f"pr-{n}.patch"
    path.write_text(patch + "\n")
    return compile_preflight.run_for_patch(n, head, path)


def _commit_message(action: str) -> str:
    return ("Rebase onto current base" if action == "rebase"
            else "Address review and CI feedback")


def push_approved(n: int) -> None:
    """Push a request an operator approved, rebuilding its worktree first.

    A mechanical change is re-derived against current base: the stored result
    proves it applied to the base it was measured against, which an active
    repository moves past continuously, so re-running the merge or rebase is what
    makes the approval mean "push this against main as it stands now". A `fix`
    re-applies the exact reviewed patch to a fresh clone of the head — the same
    bytes a person approved, never a fresh agent attempt. Both fail loudly, as a
    refusal naming what no longer fits, when the rebuild is not possible.

    Any machine can push either one, so an approval is not held hostage by the
    machine that authored it. A `resolve` is the exception: the merge commit in
    its worktree is the artifact being approved, so its own machine pushes it."""
    host = socket.gethostname()
    claimed = data.store().claim_fix_request(n, host=host, statuses=("approved",),
                                             to_status="pushing")
    if claimed is None:
        return
    action = claimed.get("action") or "fix"
    authored: list[str] | None = None
    result = claimed.get("result") or {}
    if action == "resolve":
        raw_paths = result.get("conflict_paths")
        authored = [str(p) for p in raw_paths] if raw_paths else []
    elif action == "fix":
        # The agent's own paths, not the contributor's: the gate judges what
        # this push would add to the branch.
        authored = [str(c.get("path")) for c in (result.get("changes") or [])
                    if c.get("path")]
    ok, why = recheck_eligibility(n, action, authored)
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
    elif action == "fix" and not _rebuild_fix(n, claimed, result):
        return
    _push(n, claimed, action, claimed.get("result") or {})


def _rebuild_fix(n: int, claimed: dict, result: dict) -> bool:
    """Clone the contributor's head and re-apply the reviewed patch, so this
    machine holds the tree `_push` commits. Returns whether it stands ready.

    The patch is the one a person approved, applied verbatim — no agent runs
    here. `git apply` lands whole or not at all, so a refusal leaves nothing
    half-written on the branch."""
    patch = result.get("patch")
    if not isinstance(patch, str) or not patch.startswith("diff "):
        _refuse(n, claimed, "The reviewed patch is missing from this request, so "
                            "there is nothing to push — re-queue the fix.")
        return False
    prepared = _resubmit(n, "prepare")
    if prepared.returncode != 0:
        _settle(n, claimed, prepared.returncode,
                (prepared.stderr or prepared.stdout).strip())
        return False
    applied = _resubmit(n, "apply", stdin=patch)
    if applied.returncode != 0:
        _resubmit(n, "abort")
        _refuse(n, claimed,
                f"The reviewed change no longer applies to PR #{n}'s head. "
                f"Nothing was pushed — re-queue the fix to author it again.",
                result={**result, "output": (applied.stderr
                                             or applied.stdout).strip()[-TAIL_CHARS:]})
        return False
    return True


def _push(n: int, req: dict, action: str, result: dict) -> None:
    # `update` merges against current base and pushes in one command, so it is
    # its own re-derivation; the other actions push a tree already prepared. A
    # `resolve` worktree already holds its merge commit, so its push is flagless.
    args = (["update"] if action == "update" else
            ["push"] if action == "resolve" else
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
        source=req.get("source"), guidance=req.get("guidance"),
        host=socket.gethostname(), head_sha=req.get("against_head_sha"))
    data.refresh()
    _log_run(n, req, "pushed", merged.get("message"))
    if req.get("action") == "fix":
        try:
            _retrigger_review(n)
        except Exception:
            traceback.print_exc()


def _retrigger_review(n: int) -> None:
    """Ask the review provider for a fresh score on the head a fix just pushed,
    and start the backend wait that ingests it. The score is what stands between
    the pushed fix and the merge bar, so the push is what asks. Best-effort: no
    mintable token forces the executor's dry-run, and the PR waits for the next
    scheduled ingest instead."""
    token = executor.mint_bot_token()
    baseline = review_refresh.capture(n) if token else None
    res = executor.retrigger_greptile(n, token=token, dry_run=token is None)
    if res.get("status") == "executed" and baseline is not None:
        review_refresh.schedule(n, baseline)
    print(f"[fix-worker] review retrigger for PR #{n}: {res.get('status')}",
          flush=True)


# Terminal fix_request statuses. A cancelled request counts as an attempt: an
# operator saying no to this head is not an invitation to try it again.
_TERMINAL = ("pushed", "refused", "failed", "cancelled")


def _hunt_attempted(pr: Pr, action: str) -> bool:
    """Whether this PR's current head already had its unattended attempt at
    `action`. The stamp a moved head no longer matches is what re-arms the PR —
    a refused rebase stays refused until the author pushes, so re-running it
    every sweep would pin the hunter to the same few conflicted PRs. An
    operator's click is not bound by this; re-queueing by hand is how a
    restart-orphaned run is retried at the same head."""
    req = pr.fix_request or {}
    return (req.get("action") == action and req.get("status") in _TERMINAL
            and pr.head_sha is not None
            and req.get("against_head_sha") == pr.head_sha)


def _auto_fixes_in_flight() -> int:
    """How many hunter-queued fix requests sit anywhere between queued and
    pushing. Operator-queued fixes are not counted against the hunter's cap."""
    count = 0
    for rec in data.prs().values():
        req = rec.fix_request or {}
        if (req.get("source") == "auto" and req.get("action") == "fix"
                and req.get("status") in fix_queue.IN_FLIGHT):
            count += 1
    return count


def auto_fixable(pr: Pr) -> str | None:
    """The action the idle hunter would queue for this PR, or None.

    A PR GitHub reports unmergeable needs its history replayed on current base,
    which is `rebase`; a PR whose drift scan says the base moved out from under
    it needs `update`. A PR clean on both may take an agent-authored `fix` when
    the deployment opts in (TRIAGE_FIX_HUNT_FIX) and this head has not already
    burned its one unattended attempt. Anything else is left alone.

    gates.fix_huntable is the bar, not fix_eligibility: unprompted sandbox time
    goes only where a stored quality signal argues the spend is worth it."""
    if (pr.fix_request or {}).get("status") in fix_queue.IN_FLIGHT:
        return None
    if pr.mergeable is False:
        action = "rebase"
    elif pr.drift_state == "conflicts":
        action = "update"
    elif settings.fix_hunt_fix():
        action = "fix"
    else:
        return None
    if _hunt_attempted(pr, action):
        return None
    ok, _ = gates.fix_huntable(pr, action, service.changed_paths(pr))
    return action if ok else None


def _hunt_key(rec: Pr, action: str, n: int) -> tuple[int, int, float, int]:
    """Hunt priority (ascending). Mechanical actions lead: they are cheap and
    unblock the most. Fixes follow by how little they ask of the agent — a
    nits-only review, then one point below the bar, then the rest — and by
    community pain (descending) within a tier."""
    if action != "fix":
        return (0, 0, 0.0, n)
    tier = (1 if rec.review_severity == "nits"
            else 2 if rec.review_score == 4 else 3)
    pain = float(service.pr_pain(rec).get("score") or 0.0)
    return (1, tier, -pain, n)


def next_auto() -> tuple[str, int] | None:
    """The idle hunter's next (action, PR), or None when nothing is eligible."""
    fix_slots = settings.fix_hunt_limit() - _auto_fixes_in_flight()
    best: tuple[tuple[int, int, float, int], str, int] | None = None
    for n, rec in data.prs().items():
        action = auto_fixable(rec)
        if action is None:
            continue
        if action == "fix" and fix_slots <= 0:
            continue
        key = _hunt_key(rec, action, n)
        if best is None or key < best[0]:
            best = (key, action, n)
    return (best[1], best[2]) if best else None


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
    Returns whether the worker is running afterwards, so calling it on a live
    worker is a no-op rather than a second pair of loops. A machine that opts in
    but cannot hold the push credential safely refuses to start and says why —
    the queue API keeps serving, so the refusal never takes the app down with
    it."""
    if not enabled():
        return False
    failure = key_safety_failure()
    if failure is not None:
        print(f"[fix-worker] NOT started: {failure}", flush=True)
        return False
    if running():
        return True
    stop.clear()
    _threads[:] = [
        threading.Thread(target=_beat_loop, daemon=True, name="fix-worker-beat"),
        threading.Thread(target=_drain_loop, daemon=True, name="fix-worker"),
    ]
    for t in _threads:
        t.start()
    print(f"[fix-worker] enabled on {socket.gethostname()} as {settings.PUSH_LOGIN} "
          f"(poll every {POLL_SECONDS:.0f}s)", flush=True)
    return True
