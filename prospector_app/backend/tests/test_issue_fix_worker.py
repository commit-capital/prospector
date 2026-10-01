"""The issue-fix review lane: which requests a host takes, the replies it reads
from GitHub, the hunter's picks, and the requests it recovers from a worker
that is gone."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from issue_triage import dispute_question, fix_review, fix_review_runner, issue_links
from issue_triage.issue_store import IssueStore
from pipeline import profile, settings
from pipeline import store as S
from prospector_app.backend import data, issue_data, issue_fix_worker, lane_health

QUESTION = {"question": "2 or 3?", "options": [{"label": "A", "behavior": "2"},
                                               {"label": "B", "behavior": "3"}],
            "default": "A", "default_reason": "r"}


def _issue(store: IssueStore, n: int, *, created: str = "2026-09-20T00:00:00Z",
           grade: str = "A", labels: list[str] | None = None) -> None:
    store.save_issue({"issue": n, "meta": {"title": f"bug {n}", "state": "open",
                                           "updated_at": created, "created_at": created,
                                           "body": "b", "author": "reporter",
                                           "labels": labels or []},
                      "repro": {"grade": grade}})


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "worker_id", lambda: "studio")
    monkeypatch.setattr(lane_health, "note_success", lambda lane: None)
    monkeypatch.setattr(lane_health, "note_failure", lambda lane, **kw: None)
    s = IssueStore(tmp_path)
    for n in (1, 2):
        _issue(s, n)
    return s


def test_a_follow_up_goes_only_to_the_host_that_holds_its_run(store):
    store.edit_issue(1).record_fix_run({"ending": "fixed", "patch": "p", "host": "laptop"})
    fix_review.queue(store, 1, "propose", by="op")
    assert issue_fix_worker.next_request(store.all_issues(), "studio") is None
    assert issue_fix_worker.next_request(store.all_issues(), "laptop") == 1


def test_any_host_takes_a_solve(store):
    fix_review.queue(store, 2, "solve", by="op")
    assert issue_fix_worker.next_request(store.all_issues(), "studio") == 2


def test_run_once_claims_carries_out_and_books_the_ending(store, monkeypatch):
    seen = {}

    def run_request(s, n, req, *, on_step):
        seen["req"] = req
        return "done", "Solved: fixed"

    monkeypatch.setattr(fix_review_runner, "run_request", run_request)
    fix_review.queue(store, 2, "solve", by="op")
    assert issue_fix_worker.run_once(store)
    assert seen["req"]["status"] == "running" and seen["req"]["host"] == "studio"
    assert not issue_fix_worker.run_once(store)


def _asked(store: IssueStore, at: str) -> None:
    store.edit_issue(1).record_fix_run({
        "ending": "fix-disputed", "host": "studio",
        "question": {**QUESTION, "asked": {"url": "u", "at": at}}})


def test_a_reporter_s_reply_queues_the_answer(store, monkeypatch):
    _asked(store, datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(dispute_question, "read_answer",
                        lambda n, **kw: {"label": "B", "login": "reporter", "url": "c"})
    assert issue_fix_worker.poll_replies(store) == 1
    req = store.load_issue(1).fix_request
    assert req["action"] == "answer" and req["answer"] == {"label": "B"}
    assert req["source"] == "reporter" and req["requested_by"] == "reporter"


def test_no_reply_waits_until_the_default_is_due(store, monkeypatch):
    monkeypatch.setattr(dispute_question, "read_answer", lambda n, **kw: None)
    _asked(store, datetime.now(timezone.utc).isoformat())
    assert issue_fix_worker.poll_replies(store) == 0
    _asked(store, (datetime.now(timezone.utc) - timedelta(days=8)).isoformat())
    assert issue_fix_worker.poll_replies(store) == 1
    assert store.load_issue(1).fix_request["answer"] == {"label": "A"}


@pytest.fixture
def hunting(store, monkeypatch):
    monkeypatch.setattr(data, "prs", lambda: {})
    monkeypatch.setattr(issue_data, "issues", lambda: store.all_issues(omit_candidates=True))
    monkeypatch.setattr(issue_links, "linked_prs", lambda issue, links: [])
    now = datetime.now(timezone.utc)
    store.save_issue({**store.load_issue(1).raw, "meta": {
        **store.load_issue(1).raw["meta"], "created_at": (now - timedelta(days=2)).isoformat()}})
    store.save_issue({**store.load_issue(2).raw, "meta": {
        **store.load_issue(2).raw["meta"], "created_at": (now - timedelta(days=1)).isoformat()}})
    return store


def test_the_hunter_queues_the_newest_fresh_issue(hunting):
    assert issue_fix_worker.hunt(hunting) == 2
    req = hunting.load_issue(2).fix_request
    assert req["action"] == "solve" and req["source"] == "hunter"


def test_the_hunter_skips_linked_poorly_reproduced_and_feature_issues(hunting, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    _issue(hunting, 3, created=now, grade="D")
    _issue(hunting, 4, created=now, labels=["enhancement"])
    monkeypatch.setattr(issue_links, "linked_prs",
                        lambda issue, links: [{"pr": 5}] if issue.number == 2 else [])
    assert issue_fix_worker.hunt(hunting) == 1


def test_the_hunter_keeps_to_its_daily_budget(hunting, monkeypatch):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_HUNT_BUDGET", "1")
    assert issue_fix_worker.hunt(hunting) == 2
    assert issue_fix_worker.hunt(hunting) is None


def test_the_hunter_takes_a_maintainer_s_issue_ahead_of_a_newer_one(hunting):
    raw = hunting.load_issue(1).raw
    hunting.save_issue({**raw, "meta": {**raw["meta"], "author_association": "MEMBER"}})
    assert issue_fix_worker.hunt(hunting) == 1


def test_a_priority_author_s_queued_request_runs_first(store, monkeypatch):
    fix_review.queue(store, 1, "solve", by="op")
    fix_review.queue(store, 2, "solve", by="op")
    raw = store.load_issue(2).raw
    store.save_issue({**raw, "meta": {**raw["meta"], "author": "eager-dev"}})
    assert issue_fix_worker.next_request(store.all_issues(), "studio") == 1
    monkeypatch.setattr(profile, "active",
                        lambda: profile.RepoProfile(priority_authors=("eager-dev",)))
    assert issue_fix_worker.next_request(store.all_issues(), "studio") == 2


def _ago(**kw: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def _failed(store: IssueStore, n: int, *, finished: str, attempts: int = 1,
            guidance: str | None = None) -> None:
    req = {"action": "solve", "status": "failed", "source": "hunter", "requested_by": "hunter",
           "queued_at": finished, "started_at": finished, "finished_at": finished,
           "attempts": attempts, "reason": "interrupted: the issue-fix worker restarted"}
    if guidance:
        req["guidance"] = guidance
    store.edit_issue(n).record_fix_request(req)


def test_the_hunter_rests_a_failed_solve_before_retrying_it(hunting):
    _failed(hunting, 2, finished=_ago(minutes=5))
    assert issue_fix_worker.hunt(hunting) == 1


def test_the_hunter_retries_a_failed_solve_once_it_has_rested(hunting):
    _failed(hunting, 2, finished=_ago(hours=2))
    assert issue_fix_worker.hunt(hunting) == 2
    req = hunting.load_issue(2).fix_request
    assert req["status"] == "queued" and req["source"] == "hunter" and req["attempts"] == 2


def test_the_hunter_stops_retrying_after_its_attempts(hunting):
    _failed(hunting, 2, finished=_ago(hours=2), attempts=issue_fix_worker.HUNT_MAX_ATTEMPTS)
    assert issue_fix_worker.hunt(hunting) == 1


def test_the_hunter_leaves_a_failed_guided_solve_to_the_operator(hunting):
    _failed(hunting, 2, finished=_ago(hours=2), guidance="look at x")
    assert issue_fix_worker.hunt(hunting) == 1


@pytest.fixture
def beats(store, tmp_path, monkeypatch):
    """The shared store the lane's heartbeats live in."""
    st = S.Store(tmp_path / "prs")
    monkeypatch.setattr(data, "_store", st)
    return st


def _claimed(store: IssueStore, n: int, host: str, *, at: str | None = None) -> None:
    fix_review.queue(store, n, "solve", by="op")
    store.claim_fix_request(n, host=host)
    if at:
        issue = store.edit_issue(n)
        issue.record_fix_request({**issue.fix_request, "started_at": at})


def test_the_heartbeat_names_the_issue_in_flight(store, beats, monkeypatch):
    monkeypatch.setitem(issue_fix_worker.state, "current", 1)
    issue_fix_worker.beat()
    rec = beats.load_issue_fix_worker()["hosts"]["studio"]
    assert rec["current_issue"] == 1 and rec["last_beat"]


def test_a_lane_that_has_drained_drops_its_heartbeat(store, beats, monkeypatch):
    issue_fix_worker.beat()
    drained = threading.Event()
    drained.set()
    monkeypatch.setattr(issue_fix_worker, "_drained", drained)
    issue_fix_worker._beat_loop()
    assert "studio" not in beats.load_issue_fix_worker()["hosts"]


def test_a_claim_this_host_made_before_the_process_started_ends_failed(store, beats, monkeypatch):
    beats.save_issue_fix_worker({"host": "studio", "last_beat": _ago()})
    _claimed(store, 1, "studio", at=_ago(minutes=5))
    monkeypatch.setattr(issue_fix_worker, "STARTED_AT", _ago(minutes=1))
    assert issue_fix_worker.recover_orphans(store) == [1]
    issue = store.load_issue(1)
    status, reason = fix_review.fix_status(issue)
    assert status == "failed" and reason.startswith("interrupted:") and "restarted" in reason
    assert issue.fix_request["finished_at"]
    assert issue.fix_thread[-1]["by"] == "worker" and issue.fix_thread[-1]["text"] == reason
    assert fix_review.queue(store, 1, "solve", by="op")[0]


def test_a_claim_this_process_made_is_left_running(store, beats, monkeypatch):
    monkeypatch.setattr(issue_fix_worker, "STARTED_AT", _ago(minutes=1))
    _claimed(store, 1, "studio")
    assert issue_fix_worker.recover_orphans(store) == []
    assert store.load_issue(1).fix_request["status"] == "running"


@pytest.mark.parametrize("beat,claimed,want", [
    (_ago(hours=2), _ago(hours=3), [1]),
    (_ago(minutes=1), _ago(hours=3), []),
    (None, _ago(hours=2), [1]),
    (None, _ago(minutes=5), []),
], ids=["silent", "live", "never-beat-old-claim", "never-beat-fresh-claim"])
def test_another_host_s_claim_ends_failed_once_its_heartbeat_is_silent(
        store, beats, beat, claimed, want):
    if beat:
        beats.save_issue_fix_worker({"host": "laptop", "last_beat": beat})
    _claimed(store, 1, "laptop", at=claimed)
    assert issue_fix_worker.recover_orphans(store) == want
    req = store.load_issue(1).fix_request
    if want:
        assert req["status"] == "failed" and "laptop went offline" in req["reason"]
    else:
        assert req["status"] == "running"


def test_recovery_leaves_a_request_that_ended_since_it_was_read(store, beats, monkeypatch):
    _claimed(store, 1, "laptop", at=_ago(hours=3))
    real = store.issues_matching

    def read_then_finish(path, values):
        found = real(path, values)
        issue = store.edit_issue(1)
        issue.record_fix_request({**issue.fix_request, "status": "done"})
        return found

    monkeypatch.setattr(store, "issues_matching", read_then_finish)
    assert issue_fix_worker.recover_orphans(store) == []
    assert store.load_issue(1).fix_request["status"] == "done"
