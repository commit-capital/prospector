"""The issue-fix review lane (`TRIAGE_ISSUE_FIX_WORKER=1`): drain the issues'
fix requests on the machine that holds the sandbox.

`settings.issue_fix_slots()` slot threads each claim and carry out one request
at a time, the oldest queued first. A `solve` may run on any issue-fix worker;
every other action needs the files and held base of the run it acts on, so only
the host that run names takes it. A claim is a compare-and-swap
(`IssueStore.claim_fix_request`), so two slots or machines never both take one.
`fix_review_runner.run_request` carries it out. Beside the slots, one
coordinator thread reads the replies to questions asked on GitHub every half
hour (`poll_replies`); every ten minutes it follows up its open pull requests
(`issue_triage.followup`, as `TRIAGE_ISSUE_FIX_FOLLOWUP` allows), then ingests
the in-scope issues updated since, starts another attempt where a reply or an
edited report calls for one, and brings GitHub in line with their attempts
(`issue_triage.public_loop`, as `TRIAGE_ISSUE_FIX_PUBLIC` allows); and, with
`TRIAGE_ISSUE_FIX_HUNT=1` and a slot free, it queues one `solve` for a fresh
issue (`hunt`) within `settings.issue_fix_hunt_budget()` a UTC day, from the
issues the triage judged fixable (`triage`, TRIAGE_BATCH of the newest before
each hunt; the older backlog once, by `prospector triage-backfill`). The lane
books every ending on the machine's `issue-fix` health and picks nothing while
that lane is tripped; only the coordinator retests it. Agent work no operator
asked for — a request automation queued, the hunter's pick, the routing of
replies — waits while the machine's AI capacity is paused
(`lane_health.capacity_open`); an operator's request runs regardless.

A beat thread writes the lane's heartbeat to the shared store for as long as
the coordinator or any slot runs. A request does not survive the process
carrying it out, so on start and every RECLAIM_SECONDS the lane queues again,
attempts unchanged, each `running` request no worker will finish
(`recover_orphans`).
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from issue_triage import (
    dispute_question,
    fix_choice,
    fix_review,
    fix_review_runner,
    fixability,
    followup,
    public_loop,
)
from issue_triage.issue_model import Issue
from issue_triage.issue_store import IssueStore
from pipeline import (capacity, gates, headless_agent, prove, settings, storekit, verdict_log,
                      verify_driver)
from pipeline.store import Store
from prospector_app.backend import (data, lane_health, profile_gate, schema_gate, verify_worker, worker_log,
                                    worker_loops)

LANE = "issue-fix"
POLL_SECONDS = 20.0
# How soon a slot looks again after it ran a request or lost its pick to another.
BUSY_POLL_SECONDS = 1.0
REPLY_POLL_SECONDS = 30 * 60
# How often an idle worker follows up the pull requests it proposed and syncs
# the public loop.
FOLLOWUP_POLL_SECONDS = 10 * 60
SHUTDOWN_TIMEOUT = 10.0
# How recent an issue the hunter picks.
HUNT_MAX_AGE = timedelta(days=30)
HUNT_SKIP_LABELS = frozenset({"enhancement", "documentation", "question"})
# A failed request is the machine's (a base or sandbox that could not run),
# never a verdict on the issue. The hunter queues a failed `solve` again once it
# has rested this long, up to HUNT_MAX_ATTEMPTS tries of the issue.
FAILED_RETRY_COOLDOWN = timedelta(hours=1)
HUNT_MAX_ATTEMPTS = 3
# How often the coordinator looks for requests a worker that is gone left running.
RECLAIM_SECONDS = 300.0
# The request actions that run no agent: a `propose` pushes a proven patch and
# opens its pull request.
MECHANICAL_ACTIONS = frozenset({"propose"})

# This process's start. A `running` claim this host stamped before it was made by
# a process that is gone.
STARTED_AT = storekit.now()

stop = threading.Event()
# Set once the coordinator and every slot have ended, their requests in flight
# finished; the heartbeat beats until then.
_drained = threading.Event()
_threads: list[threading.Thread] = []
_state_lock = threading.Lock()
state: dict[str, set[int]] = {"current": set()}


def enabled() -> bool:
    return settings.issue_fix_worker_enabled()


def running() -> bool:
    return any(t.is_alive() for t in _threads)


def startup() -> bool:
    """Start the lane's coordinator, slot and beat threads when the flag is on.
    Returns whether it runs."""
    if not enabled():
        return False
    if running():
        return True
    worker_log.install()
    stop.clear()
    _drained.clear()
    slots = settings.issue_fix_slots()
    if slots > 1 and slots > (large := verify_driver.large_slots()):
        print(f"[issue-fix] warning: {slots} issue-fix slots but {large} large sandbox "
              f"slots; the extra slots wait on the sandbox", flush=True)
    workers = [threading.Thread(target=_drain, args=(slots,), name="issue-fix-worker",
                      daemon=True),
               *(threading.Thread(target=_slot_loop, name=f"issue-fix-slot-{i}", daemon=True)
                 for i in range(slots))]
    _threads[:] = [*workers, threading.Thread(target=_beat_loop, args=(workers,),
                                              name="issue-fix-worker-beat", daemon=True)]
    for t in _threads:
        t.start()
    return True


def shutdown(timeout: float = SHUTDOWN_TIMEOUT) -> bool:
    """Signal the lane to stop and wait up to `timeout` for it; the requests in
    flight finish."""
    stop.set()
    deadline = time.monotonic() + timeout
    for t in _threads:
        t.join(max(0.0, deadline - time.monotonic()))
    alive = running()
    if not alive:
        _threads.clear()
    return not alive


def in_flight() -> list[int]:
    """The issues this process's slots are carrying out."""
    with _state_lock:
        return sorted(state["current"])


def beat() -> None:
    """Write this lane's liveness, the issues in flight, and whether its hunter
    is on to the shared store."""
    ids = in_flight()
    data.store().save_issue_fix_worker({
        "host": settings.worker_id(), "pid": os.getpid(), "last_beat": storekit.now(),
        "current_issue": min(ids) if ids else None, "current_issues": ids,
        "autohunt": settings.issue_fix_hunt()})


def _beat_loop(workers: list[threading.Thread]) -> None:
    """Beat until every one of `workers` has ended, then drop the heartbeat, so
    a lane switched off is not read as one that went dark."""
    while any(t.is_alive() for t in workers):
        try:
            if not schema_gate.paused("issue-fix"):
                beat()
        except Exception:
            traceback.print_exc()
        deadline = time.monotonic() + POLL_SECONDS
        for t in workers:
            t.join(max(0.0, deadline - time.monotonic()))
    _drained.set()
    try:
        data.store().clear_issue_fix_worker(settings.worker_id())
    except Exception:
        traceback.print_exc()


def recover_orphans(store: IssueStore) -> list[int]:
    """Queue again the `running` requests no worker will finish: this host's
    own claimed before this process started, and any host's whose issue-fix
    heartbeat has been silent past worker_health.OFFLINE_AFTER_SECONDS (a host
    with no heartbeat is judged by its claim's age). A claim a live lane holds
    is its run, never touched from here, and a request that ended since it was
    read is left as it ended. Returns the issues requeued."""
    me = settings.worker_id()
    registry = data.store().load_issue_fix_worker()
    requeued_issues: list[int] = []
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
        note = f"requeued: {who} mid-{req.get('action')}"
        got = store.stamped_issue(n)
        if got is None:
            continue
        current, stamp = got
        if current.fix_request != req:
            continue
        now = storekit.now()
        requeued = {k: v for k, v in req.items() if k not in ("host", "started_at")}
        current.stage_fix_request({**requeued, "status": "queued"})
        current.stage_fix_thread({"at": now, "by": "worker", "kind": "note", "text": note})
        if store.save_issue_if(current, stamp):
            requeued_issues.append(n)
    return requeued_issues


def _unattended(req: dict, run: dict | None) -> bool:
    """Whether request `req` on the attempt `run` is agent work no operator
    queued. An `ask-reporter` runs an agent only to draft a question `run` does
    not already hold (`executor.ask_issue_question`)."""
    if req.get("source") == "operator" or req.get("action") in MECHANICAL_ACTIONS:
        return False
    return not (req.get("action") == "ask-reporter"
                and ((run or {}).get("question") or {}).get("question"))


def next_request(issues: dict, host: str) -> int | None:
    """The oldest queued request this host may take; one that is unattended
    agent work only while the lane's AI capacity is open."""
    for n in fix_review.queued_issues(issues):
        req = issues[n].fix_request or {}
        if req.get("action") != "solve" and (issues[n].fix_run or {}).get("host") != host:
            continue
        if _unattended(req, issues[n].fix_run) and not lane_health.capacity_open(LANE):
            continue
        return n
    return None


def run_once(store: IssueStore) -> bool:
    """Claim and carry out one request. Whether to look again at once: one
    ran, or another slot or machine claimed the pick first."""
    host = settings.worker_id()
    # Only the queued rows, filtered server-side: this runs every POLL_SECONDS
    # on every worker, and reading the whole issue table each tick was most of
    # the store's egress.
    issues = store.issues_matching(("fix_request", "status"), ["queued"])
    n = next_request(issues, host)
    if n is None:
        return False
    req = issues[n].fix_request or {}
    hosts = None if req.get("action") == "solve" else frozenset({host})
    claimed = store.claim_fix_request(n, host=host, hosts=hosts)
    if claimed is None:
        return True
    with _state_lock:
        state["current"].add(n)
    print(f"[issue-fix] #{n}: {claimed['action']}", flush=True)
    metering = (capacity.metered(LANE) if _unattended(claimed, issues[n].fix_run)
                else contextlib.nullcontext())
    try:
        with metering:
            status, outcome = fix_review_runner.run_request(
                store, n, claimed, on_step=lambda s: print(f"[issue-fix] #{n}: {s}", flush=True))
    except headless_agent.AgentUnavailable as e:
        lane_health.trip_agent_lanes(str(e))
        return True
    finally:
        with _state_lock:
            state["current"].discard(n)
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
    for n, issue in store.fix_issues().items():
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


def _budget_spent(store: IssueStore) -> bool:
    since = storekit.utc_midnight().isoformat(timespec="seconds")
    return store.hunted_since(since) >= settings.issue_fix_hunt_budget()


def _retryable(req: dict | None) -> bool:
    """Whether the hunter may queue a `solve` over the issue's request `req`:
    there is none, or it is a `solve` that failed with no guidance or notes to
    carry, has rested FAILED_RETRY_COOLDOWN, and has attempts left."""
    if not req:
        return True
    if (req.get("action") != "solve" or req.get("status") != "failed"
            or req.get("guidance") or req.get("notes")):
        return False
    if int(req.get("attempts") or 1) >= HUNT_MAX_ATTEMPTS:
        return False
    age = storekit.seconds_since(req.get("finished_at"))
    return age is None or age >= FAILED_RETRY_COOLDOWN.total_seconds()


def _huntable(issues: dict) -> list[tuple[bool, int, Issue]]:
    """The open issues the hunter may take, as (maintainer's, number, issue):
    filed within HUNT_MAX_AGE, carrying no HUNT_SKIP_LABELS, with no attempt
    or chosen pull request, or one whose last `solve` failed and may be
    retried."""
    since = (datetime.now(timezone.utc) - HUNT_MAX_AGE).isoformat()
    out = []
    for n, i in issues.items():
        meta = i.raw.get("meta") or {}
        if (meta.get("state") != "open" or (meta.get("created_at") or "") < since
                or HUNT_SKIP_LABELS & set(meta.get("labels") or [])
                or i.fix_run or fix_choice.chosen(i) is not None or not _retryable(i.fix_request)):
            continue
        out.append((gates.priority_author(i.author, i.author_association), n, i))
    return out


# Issues whose triage the agent failed this process, by when, so a run that
# cannot finish is not retried every tick.
_triage_failed: dict[int, float] = {}
TRIAGE_RETRY_SECONDS = 6 * 3600
# Issues triaged before each hunt: above the daily intake over the day's hunts.
TRIAGE_BATCH = 5


def triage(store: IssueStore, limit: int | None, *, trigger: str = "worker") -> list[int]:
    """Judge up to `limit` (None: every) huntable issues with no current
    fixability triage, a maintainer's and the newest first, over this
    machine's pinned base, each while the lane's AI capacity is open, and post
    the question an `ask` carries (`public_loop.ask_triage`). The issues
    judged. A base this machine lacks leaves every issue untriaged; an agent
    outage propagates."""
    from prospector_app.backend import issue_data

    if not lane_health.capacity_open(LANE):
        return []
    now = time.monotonic()
    pending = sorted(((m, (i.raw.get("meta") or {}).get("created_at") or "", n, i)
                      for m, n, i in _huntable(issue_data.issues())
                      if fixability.current(i) is None
                      and now - _triage_failed.get(n, -TRIAGE_RETRY_SECONDS) >= TRIAGE_RETRY_SECONDS),
                     key=lambda p: p[:3], reverse=True)[:limit]
    if not pending:
        return []
    try:
        base = prove.pinned(Store())
    except prove.NoBase:
        return []
    judged = []
    for *_, n, issue in pending:
        if not lane_health.capacity_open(LANE):
            break
        if _judge(store, issue, base, trigger):
            judged.append(n)
    return judged


def _judge(store: IssueStore, issue: Issue, base: prove.PinnedBase, trigger: str) -> bool:
    """Triage one issue and book it. Whether the agent finished."""
    n = issue.number
    started = storekit.now()
    created = storekit.parse_ts((issue.raw.get("meta") or {}).get("created_at"))
    replies = (public_loop.read_replies(issue, created) or []) if created else []
    try:
        with capacity.metered(LANE):
            record = fixability.judge(issue, base.clone, base_sha=base.sha, replies=replies,
                                      subject=verdict_log.Subject("issue", n))
    except (ValueError, RuntimeError) as e:
        if isinstance(e, headless_agent.AgentUnavailable):
            raise
        _triage_failed[n] = time.monotonic()
        traceback.print_exc()
        return False
    record["asks"] = int((issue.fix_triage or {}).get("asks") or 0)
    if record["verdict"] == "ask":
        record["asked"] = public_loop.ask_triage(issue, record)
        record["asks"] += record["asked"] == "posted"
    store.edit_issue(n).record_fix_triage(record)
    store.append_run({"phase": "issue-triage:run", "issue": n, "started": started,
                      "finished": storekit.now(), "trigger": trigger,
                      "stats": {"verdict": record["verdict"], "kind": record.get("kind"),
                                "fixability": record.get("fixability"),
                                "easy": fixability.easy(record),
                                "asked": record.get("asked")}})
    return True


def fill(store: IssueStore, want: int) -> list[int]:
    """Queue up to `want` `solve`s for the most promising huntable issues the
    fixability triage judged `go` at their current content, a maintainer's
    ahead of any other, then an easy fix, then by fixability, then the newest
    (`fixability.hunt_key`), never past the day's budget and only while the
    lane's AI capacity is open. The triage's replies ride as notes. The issues
    queued. Picks come from the app's issue snapshot; the day's count is read
    from the store, and each queue write re-checks the issue there."""
    from prospector_app.backend import issue_data

    queued: list[int] = []
    issues = issue_data.issues()
    picks = [(fixability.hunt_key(i, maintainer=m), n, fixability.current(i) or {})
             for m, n, i in _huntable(issues)]
    for _, n, tri in sorted((p for p in picks if p[2].get("verdict") == "go"), reverse=True):
        if len(queued) >= want:
            break
        if _budget_spent(store) or not lane_health.capacity_open(LANE):
            break
        ok, _ = fix_review.queue(store, n, "solve", by="hunter", source="hunter",
                                 guidance=tri.get("guidance"), notes=tri.get("notes"))
        if ok:
            queued.append(n)
    return queued


def hunt(store: IssueStore) -> int | None:
    """`fill` for one slot: the issue queued, or None."""
    return next(iter(fill(store, 1)), None)


def _follow_up(store: IssueStore) -> None:
    """`followup.poll`, whose reply routing runs an agent only while the lane's
    AI capacity is open; its other steps run regardless."""
    with capacity.metered(LANE):
        followup.poll(store, may_route=lambda: lane_health.capacity_open(LANE))


def _answer_replies(store: IssueStore) -> None:
    """`public_loop.answer_replies`, whose reply routing runs an agent only while
    the lane's AI capacity is open; reading replies and report edits runs
    regardless."""
    with capacity.metered(LANE):
        public_loop.answer_replies(store, may_route=lambda: lane_health.capacity_open(LANE))


def _every_ten_minutes(store: IssueStore) -> None:
    """Follow up the open proposals, then refresh the in-scope issues, answer
    the replies, and bring GitHub in line with them; one step failing leaves the
    others to run, and an agent outage trips the lanes and ends the pass."""
    try:
        for step in (_follow_up, public_loop.refresh, _answer_replies, public_loop.sync):
            try:
                step(store)
            except headless_agent.AgentUnavailable as e:
                lane_health.trip_agent_lanes(str(e))
                return
            except Exception:
                traceback.print_exc()
    finally:
        worker_loops.ran("issue-followup", FOLLOWUP_POLL_SECONDS)


def _reclaim(store: IssueStore) -> None:
    requeued = recover_orphans(store)
    if requeued:
        print(f"[issue-fix] requeued, left running by a worker that is gone: {requeued}",
              flush=True)


def _slot_loop() -> None:
    """Claim and carry out requests, one at a time, until `stop`."""
    store = IssueStore()
    while not stop.is_set():
        ran = False
        try:
            if (not schema_gate.paused("issue-fix") and not profile_gate.paused("issue-fix")
                    and lane_health.is_open(LANE)):
                ran = run_once(store)
        except Exception:
            traceback.print_exc()
        stop.wait(BUSY_POLL_SECONDS if ran else POLL_SECONDS)


def _idle(store: IssueStore, slots: int) -> int:
    """The slots no request in flight or queued `solve` will fill; at or below
    zero when none is idle."""
    queued_solves = sum(
        1 for i in store.issues_matching(("fix_request", "status"), ["queued"]).values()
        if (i.fix_request or {}).get("action") == "solve")
    return slots - len(in_flight()) - queued_solves


def _drain(slots: int) -> None:
    """The coordinator: reclaim, replies, the ten-minute pass, triage and the
    hunt, each on its own timer while the `slots` slot threads run."""
    store = IssueStore()
    last_replies = 0.0
    last_followup = 0.0
    try:
        _reclaim(store)
    except Exception:
        traceback.print_exc()
    last_reclaim = time.monotonic()
    while not stop.is_set():
        if schema_gate.paused("issue-fix") or profile_gate.paused("issue-fix"):
            stop.wait(POLL_SECONDS)
            continue
        try:
            if time.monotonic() - last_reclaim >= RECLAIM_SECONDS:
                last_reclaim = time.monotonic()
                _reclaim(store)
            if lane_health.open_or_retest(LANE):
                if time.monotonic() - last_replies > REPLY_POLL_SECONDS:
                    poll_replies(store)
                    last_replies = time.monotonic()
                if time.monotonic() - last_followup > FOLLOWUP_POLL_SECONDS:
                    _every_ten_minutes(store)
                    last_followup = time.monotonic()
                if (settings.issue_fix_hunt() and not _budget_spent(store)
                        and (idle := _idle(store, slots)) > 0):
                    triage(store, TRIAGE_BATCH)
                    fill(store, idle)
        except Exception:
            traceback.print_exc()
        stop.wait(POLL_SECONDS)
