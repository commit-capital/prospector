"""The issue-fix review lane (`TRIAGE_ISSUE_FIX_WORKER=1`): drain the issues'
fix requests on the machine that holds the sandbox.

One request at a time, the oldest queued first. A `solve` may run on any
issue-fix worker; every other action needs the files and held base of the run it
acts on, so only the host that run names takes it. A claim is a compare-and-swap
(`IssueStore.claim_fix_request`), so two machines never both take one.
`fix_review_runner.run_request` carries it out. Between requests the lane reads
the replies to questions asked on GitHub every half hour (`poll_replies`); every
ten minutes it follows up its open pull requests (`issue_triage.followup`, as
`TRIAGE_ISSUE_FIX_FOLLOWUP` allows), then ingests the in-scope issues updated
since and brings GitHub in line with their attempts (`issue_triage.public_loop`,
as `TRIAGE_ISSUE_FIX_PUBLIC` allows); and, with `TRIAGE_ISSUE_FIX_HUNT=1`, it
queues one `solve` for a fresh issue (`hunt`) within
`settings.issue_fix_hunt_budget()` a UTC day. The lane books every ending
on the machine's `issue-fix` health and picks nothing while that lane is
tripped.

A beat thread writes the lane's heartbeat to the shared store for as long as
the drain loop runs. A request does not survive the process carrying it out, so
on start and every RECLAIM_SECONDS the lane ends `failed` each `running` request
no worker will finish (`recover_orphans`), which the operator or the hunter
then retries.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from issue_triage import (
    dispute_question,
    fix_review,
    fix_review_runner,
    followup,
    public_loop,
)
from issue_triage.issue_store import IssueStore
from pipeline import gates, headless_agent, settings, storekit
from prospector_app.backend import data, lane_health, verify_worker

LANE = "issue-fix"
POLL_SECONDS = 20.0
REPLY_POLL_SECONDS = 30 * 60
# How often an idle worker follows up the pull requests it proposed and syncs
# the public loop.
FOLLOWUP_POLL_SECONDS = 10 * 60
SHUTDOWN_TIMEOUT = 10.0
# How recent an issue the hunter picks, and the reproduction grades it trusts in
# an issue a maintainer did not file.
HUNT_MAX_AGE = timedelta(days=30)
HUNT_GRADES = frozenset({"A", "B"})
HUNT_SKIP_LABELS = frozenset({"enhancement", "documentation", "question"})
# A failed request is the machine's (a worker restarted mid-run, a base or
# sandbox that could not run), never a verdict on the issue. The hunter queues a
# failed `solve` again once it has rested this long, up to HUNT_MAX_ATTEMPTS
# tries of the issue.
FAILED_RETRY_COOLDOWN = timedelta(hours=1)
HUNT_MAX_ATTEMPTS = 3
# How often the drain loop looks for requests a worker that is gone left running.
RECLAIM_SECONDS = 300.0

# This process's start. A `running` claim this host stamped before it was made by
# a process that is gone.
STARTED_AT = storekit.now()

stop = threading.Event()
# Set when the drain loop has ended, its request in flight finished; the
# heartbeat beats until then.
_drained = threading.Event()
_threads: list[threading.Thread] = []
state: dict = {"current": None, "last_outcome": None}


def enabled() -> bool:
    return settings.issue_fix_worker_enabled()


def running() -> bool:
    return any(t.is_alive() for t in _threads)


def startup() -> bool:
    """Start the lane's drain and beat threads when the flag is on. Returns
    whether it runs."""
    if not enabled():
        return False
    if running():
        return True
    stop.clear()
    _drained.clear()
    _threads[:] = [
        threading.Thread(target=_drain_loop, name="issue-fix-worker", daemon=True),
        threading.Thread(target=_beat_loop, name="issue-fix-worker-beat", daemon=True),
    ]
    for t in _threads:
        t.start()
    return True


def shutdown(timeout: float = SHUTDOWN_TIMEOUT) -> bool:
    """Signal the lane to stop and wait for it; a request in flight finishes."""
    stop.set()
    for t in _threads:
        t.join(timeout)
    alive = running()
    if not alive:
        _threads.clear()
    return not alive


def beat() -> None:
    """Write this lane's liveness, the issue in flight, and whether its hunter is
    on to the shared store."""
    data.store().save_issue_fix_worker({
        "host": settings.worker_id(), "pid": os.getpid(), "last_beat": storekit.now(),
        "current_issue": state["current"], "autohunt": settings.issue_fix_hunt()})


def _beat_loop() -> None:
    """Beat until the drain loop has ended, then drop the heartbeat, so a lane
    switched off is not read as one that went dark."""
    while not _drained.is_set():
        try:
            beat()
        except Exception:
            traceback.print_exc()
        _drained.wait(POLL_SECONDS)
    try:
        data.store().clear_issue_fix_worker(settings.worker_id())
    except Exception:
        traceback.print_exc()


def recover_orphans(store: IssueStore) -> list[int]:
    """End `failed` the `running` requests no worker will finish: this host's
    own claimed before this process started, and any host's whose issue-fix
    heartbeat has been silent past worker_health.OFFLINE_AFTER_SECONDS (a host
    with no heartbeat is judged by its claim's age). A claim a live lane holds
    is its run, never touched from here, and a request that ended since it was
    read is left as it ended. Returns the issues marked."""
    me = settings.worker_id()
    registry = data.store().load_issue_fix_worker()
    marked: list[int] = []
    for n, issue in sorted(store.issues_matching(("fix_request", "status"), ["running"]).items()):
        req = issue.fix_request or {}
        host = req.get("host")
        claimed = req.get("started_at") or req.get("queued_at")
        if host in (None, me):
            if str(claimed or "") >= STARTED_AT:
                continue
            who = "the issue-fix worker restarted"
        elif verify_worker.worker_offline(str(host), registry, claimed):
            who = f"the issue-fix worker on {host} went offline"
        else:
            continue
        reason = f"interrupted: {who} mid-{req.get('action')}"
        got = store.stamped_issue(n)
        if got is None:
            continue
        current, stamp = got
        if current.fix_request != req:
            continue
        now = storekit.now()
        current.stage_fix_request({**req, "status": "failed", "finished_at": now,
                                   "reason": reason})
        current.stage_fix_thread({"at": now, "by": "worker", "kind": "note", "text": reason})
        if store.save_issue_if(current, stamp):
            marked.append(n)
    return marked


def next_request(issues: dict, host: str) -> int | None:
    """The oldest queued request this host may take."""
    for n in fix_review.queued_issues(issues):
        req = issues[n].fix_request or {}
        if req.get("action") == "solve" or (issues[n].fix_run or {}).get("host") == host:
            return n
    return None


def run_once(store: IssueStore) -> bool:
    """Claim and carry out one request. Whether one ran."""
    host = settings.worker_id()
    issues = store.all_issues(omit_candidates=True)
    n = next_request(issues, host)
    if n is None:
        return False
    req = issues[n].fix_request or {}
    hosts = None if req.get("action") == "solve" else frozenset({host})
    claimed = store.claim_fix_request(n, host=host, hosts=hosts)
    if claimed is None:
        return False
    state["current"] = n
    print(f"[issue-fix] #{n}: {claimed['action']}", flush=True)
    try:
        status, outcome = fix_review_runner.run_request(
            store, n, claimed, on_step=lambda s: print(f"[issue-fix] #{n}: {s}", flush=True))
    except headless_agent.AgentUnavailable as e:
        lane_health.trip_agent_lanes(str(e))
        return True
    finally:
        state["current"] = None
    state["last_outcome"] = f"#{n}: {outcome}"
    print(f"[issue-fix] #{n}: {outcome}", flush=True)
    after = store.load_issue(n)
    faulted = bool(after and (after.fix_run or {}).get("fault"))
    if status == "failed" or faulted:
        lane_health.note_failure(LANE, kind="run", reason=outcome[:300])
    else:
        lane_health.note_success(LANE)
    return True


def poll_replies(store: IssueStore) -> int:
    """Queue an `answer` for each question asked on GitHub that got one, or
    whose default is due. Returns how many were queued."""
    queued = 0
    for n, issue in store.all_issues(omit_candidates=True).items():
        run = issue.fix_run or {}
        question = run.get("question") or {}
        asked = question.get("asked") or {}
        if not asked.get("at") or question.get("answered"):
            continue
        if (issue.fix_request or {}).get("status") in fix_review.IN_FLIGHT:
            continue
        labels = [o["label"] for o in question.get("options") or []]
        got = dispute_question.read_answer(
            n, asked_at=asked["at"], options=labels,
            issue_author=(issue.raw.get("meta") or {}).get("author") or "")
        if got is None and dispute_question.default_due(asked["at"]):
            got = {"label": question.get("default"), "login": "the default after a week"}
        if got is None:
            continue
        ok, _ = fix_review.queue(store, n, "answer", by=str(got.get("login") or "reporter"),
                                 source="reporter", answer={"label": got["label"]})
        queued += ok
    return queued


def _hunted_today(issues: dict) -> int:
    midnight = storekit.utc_midnight()
    return sum(1 for i in issues.values()
               if (i.fix_request or {}).get("source") == "hunter"
               and (at := storekit.parse_ts((i.fix_request or {}).get("queued_at"))) is not None
               and at >= midnight)


def _retryable(req: dict | None) -> bool:
    """Whether the hunter may queue a `solve` over the issue's request `req`:
    there is none, or it is a `solve` that failed with no operator guidance to
    carry, has rested FAILED_RETRY_COOLDOWN, and has attempts left."""
    if not req:
        return True
    if req.get("action") != "solve" or req.get("status") != "failed" or req.get("guidance"):
        return False
    if int(req.get("attempts") or 1) >= HUNT_MAX_ATTEMPTS:
        return False
    age = storekit.seconds_since(req.get("finished_at"))
    return age is None or age >= FAILED_RETRY_COOLDOWN.total_seconds()


def hunt(store: IssueStore) -> int | None:
    """Queue a `solve` for the newest fresh issue with no linked pull request
    and no attempt, or whose last `solve` failed and may be retried, a
    maintainer's (gates.priority_author) ahead of any other, within the day's
    budget. Another author's issue must be well reproduced (HUNT_GRADES); a
    maintainer's is taken as reported. The issue queued, or None. Reads the
    app's issue and PR snapshots; the queue write re-checks the issue on the
    store."""
    from issue_triage import issue_links, pr_index
    from prospector_app.backend import issue_data

    issues = issue_data.issues()
    if _hunted_today(issues) >= settings.issue_fix_hunt_budget():
        return None
    since = (datetime.now(timezone.utc) - HUNT_MAX_AGE).isoformat()
    picks = []
    for n, i in issues.items():
        meta = i.raw.get("meta") or {}
        maintainer = gates.priority_author(i.author, i.author_association)
        if (meta.get("state") != "open" or (meta.get("created_at") or "") < since
                or (not maintainer
                    and (i.raw.get("repro") or {}).get("grade") not in HUNT_GRADES)
                or HUNT_SKIP_LABELS & set(meta.get("labels") or [])
                or i.fix_run or not _retryable(i.fix_request)):
            continue
        picks.append((maintainer, meta.get("created_at") or "", n))
    index = pr_index.build(data.prs().values())
    for *_, n in sorted(picks, reverse=True):
        if issue_links.linked_prs(issues[n], index.get(n)):
            continue
        ok, _ = fix_review.queue(store, n, "solve", by="hunter", source="hunter")
        if ok:
            return n
    return None


def _every_ten_minutes(store: IssueStore) -> None:
    """Follow up the open proposals, then refresh the in-scope issues and bring
    GitHub in line with them; one step failing leaves the others to run."""
    for step in (followup.poll, public_loop.refresh, public_loop.sync):
        try:
            step(store)
        except Exception:
            traceback.print_exc()


def _reclaim(store: IssueStore) -> None:
    marked = recover_orphans(store)
    if marked:
        print(f"[issue-fix] marked failed, left running by a worker that is gone: {marked}",
              flush=True)


def _drain_loop() -> None:
    try:
        _drain()
    finally:
        _drained.set()


def _drain() -> None:
    store = IssueStore()
    last_replies = 0.0
    last_followup = 0.0
    try:
        _reclaim(store)
    except Exception:
        traceback.print_exc()
    last_reclaim = time.monotonic()
    while not stop.is_set():
        ran = False
        try:
            if time.monotonic() - last_reclaim >= RECLAIM_SECONDS:
                last_reclaim = time.monotonic()
                _reclaim(store)
            if lane_health.open_or_retest(LANE):
                ran = run_once(store)
                if not ran:
                    if time.monotonic() - last_replies > REPLY_POLL_SECONDS:
                        poll_replies(store)
                        last_replies = time.monotonic()
                    if time.monotonic() - last_followup > FOLLOWUP_POLL_SECONDS:
                        _every_ten_minutes(store)
                        last_followup = time.monotonic()
                    if settings.issue_fix_hunt():
                        hunt(store)
        except Exception:
            traceback.print_exc()
        stop.wait(1.0 if ran else POLL_SECONDS)
