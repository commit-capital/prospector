"""The issue-fix review lane: which requests a host takes, the replies it reads
from GitHub, and the hunter's picks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_triage import dispute_question, fix_review, fix_review_runner, issue_links, pr_index
from issue_triage.issue_store import IssueStore
from pipeline import settings
from prospector_app.backend import issue_fix_worker, lane_health

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
    monkeypatch.setattr(pr_index, "from_store", lambda: {})
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
