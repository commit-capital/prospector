"""The issue-fix review lane (`TRIAGE_ISSUE_FIX_WORKER=1`): drain the issues'
fix requests on the machine that holds the sandbox.

One request at a time, the oldest queued first. A `solve` may run on any
issue-fix worker; every other action needs the files and held base of the run it
acts on, so only the host that run names takes it. A claim is a compare-and-swap
(`IssueStore.claim_fix_request`), so two machines never both take one.
`fix_review_runner.run_request` carries it out. Between requests the lane reads
the replies to questions asked on GitHub every half hour (`poll_replies`) and,
with `TRIAGE_ISSUE_FIX_HUNT=1`, queues one `solve` for a fresh issue (`hunt`)
within `settings.issue_fix_hunt_budget()` a UTC day. The lane books every ending
on the machine's `issue-fix` health and picks nothing while that lane is
tripped.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from issue_triage import dispute_question, fix_review, fix_review_runner
from issue_triage.issue_store import IssueStore
from pipeline import headless_agent, settings
from prospector_app.backend import lane_health

LANE = "issue-fix"
POLL_SECONDS = 20.0
REPLY_POLL_SECONDS = 30 * 60
SHUTDOWN_TIMEOUT = 10.0
# How recent an issue the hunter picks, and the reproduction grades it trusts.
HUNT_MAX_AGE = timedelta(days=30)
HUNT_GRADES = frozenset({"A", "B"})
HUNT_SKIP_LABELS = frozenset({"enhancement", "documentation", "question"})

stop = threading.Event()
_threads: list[threading.Thread] = []
state: dict = {"current": None, "last_outcome": None}


def enabled() -> bool:
    return settings.issue_fix_worker_enabled()


def running() -> bool:
    return any(t.is_alive() for t in _threads)


def startup() -> bool:
    """Start the lane's thread when the flag is on. Returns whether it runs."""
    if not enabled():
        return False
    if running():
        return True
    stop.clear()
    _threads[:] = [threading.Thread(target=_drain_loop, name="issue-fix-worker", daemon=True)]
    _threads[0].start()
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
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(1 for i in issues.values()
               if (i.fix_request or {}).get("source") == "hunter"
               and str((i.fix_request or {}).get("queued_at") or "").startswith(today))


def hunt(store: IssueStore) -> int | None:
    """Queue a `solve` for the newest fresh, well-reproduced issue with no
    linked pull request and no attempt, within the day's budget. The issue
    queued, or None."""
    from issue_triage import issue_links, pr_index

    issues = store.all_issues(omit_candidates=True)
    if _hunted_today(issues) >= settings.issue_fix_hunt_budget():
        return None
    since = (datetime.now(timezone.utc) - HUNT_MAX_AGE).isoformat()
    picks = []
    for n, i in issues.items():
        meta = i.raw.get("meta") or {}
        if (meta.get("state") != "open" or (meta.get("created_at") or "") < since
                or (i.raw.get("repro") or {}).get("grade") not in HUNT_GRADES
                or HUNT_SKIP_LABELS & set(meta.get("labels") or [])
                or i.fix_run or i.fix_request):
            continue
        picks.append((meta.get("created_at") or "", n))
    index = pr_index.from_store()
    for _, n in sorted(picks, reverse=True):
        if issue_links.linked_prs(issues[n], index.get(n)):
            continue
        ok, _ = fix_review.queue(store, n, "solve", by="hunter", source="hunter")
        if ok:
            return n
    return None


def _drain_loop() -> None:
    store = IssueStore()
    last_replies = 0.0
    while not stop.is_set():
        ran = False
        try:
            if lane_health.open_or_retest(LANE):
                ran = run_once(store)
                if not ran:
                    if time.monotonic() - last_replies > REPLY_POLL_SECONDS:
                        poll_replies(store)
                        last_replies = time.monotonic()
                    if settings.issue_fix_hunt():
                        hunt(store)
        except Exception:
            traceback.print_exc()
        stop.wait(1.0 if ran else POLL_SECONDS)
