"""Reviewing issue fixes in the app: the queue's rules, the claim, the derived
status, the distilled run, and the runner's dispatch."""
from __future__ import annotations

import pytest

from issue_triage import fix_review, fix_review_runner
from issue_triage.issue_store import IssueStore
from pipeline import settings

QUESTION = {"question": "Should x read 2 or 3?",
            "options": [{"label": "A", "behavior": "2"}, {"label": "B", "behavior": "3"}],
            "default": "A", "default_reason": "the report says 2"}


@pytest.fixture
def store(tmp_path):
    s = IssueStore(tmp_path)
    s.save_issue({"issue": 7, "meta": {"title": "x is wrong", "state": "open",
                                       "updated_at": "2026-09-01T00:00:00Z", "body": "b",
                                       "author": "reporter"}})
    return s


def _run(store: IssueStore, **over) -> None:
    run = {"ending": "fixed", "detail": "ok", "patch": "diff --git a/x b/x\n", "host": "studio",
           "question": None, "proposal": None, **over}
    store.edit_issue(7).record_fix_run(run)


def test_a_fresh_issue_has_no_status_and_may_be_solved(store):
    assert fix_review.fix_status(store.load_issue(7)) is None
    assert fix_review.queue(store, 7, "solve", by="op", guidance="look at x") == (
        True, "solve queued")
    issue = store.load_issue(7)
    assert fix_review.fix_status(issue) == ("running", "solve queued")
    assert issue.fix_request["guidance"] == "look at x"
    assert issue.fix_thread[-1]["text"] == "look at x" and issue.fix_thread[-1]["by"] == "op"


def test_one_request_at_a_time(store):
    fix_review.queue(store, 7, "solve", by="op")
    ok, why = fix_review.queue(store, 7, "solve", by="op")
    assert not ok and "already queued" in why


@pytest.mark.parametrize("action,kw,run,why", [
    ("send-back", {"guidance": ""}, {}, "needs your comments"),
    ("send-back", {"guidance": "g"}, {"patch": ""}, "no change"),
    ("answer", {"answer": {"label": "A"}}, {}, "asks no question"),
    ("answer", {"answer": {"label": "C"}},
     {"ending": "fix-disputed", "question": QUESTION}, "names one of"),
    ("ask-reporter", {}, {"ending": "fix-disputed",
                          "question": {**QUESTION, "asked": {"at": "t"}}}, "already asked"),
    ("propose", {}, {"ending": "fix-disputed"}, "not 'fixed'"),
    ("propose", {}, {"proposal": {"pr": 9}}, "already open"),
])
def test_an_action_that_does_not_fit_the_attempt_is_refused(store, action, kw, run, why):
    _run(store, **run)
    ok, reason = fix_review.queue(store, 7, action, by="op", **kw)
    assert not ok and why in reason


def test_an_answer_names_an_option_or_says_what_should_happen(store):
    _run(store, ending="fix-disputed", question=QUESTION)
    assert fix_review.queue(store, 7, "answer", by="op", answer={"label": "B"})[0]
    assert store.load_issue(7).fix_thread[-1]["text"] == "Answered B"
    fix_review.cancel(store, 7, by="op")
    assert fix_review.queue(store, 7, "answer", by="op", answer={"text": "x reads 2"})[0]


def test_a_closed_issue_takes_no_request(store):
    issue = store.edit_issue(7)
    issue.set_meta({**issue.raw["meta"], "state": "closed"})
    assert not fix_review.queue(store, 7, "solve", by="op")[0]


@pytest.mark.parametrize("run,req,want", [
    ({"ending": "fixed"}, None, "review"),
    ({"ending": "fix-disputed", "question": QUESTION}, None, "question"),
    ({"ending": "fix-disputed", "question": {**QUESTION, "asked": {"at": "t"}}}, None,
     "reporter"),
    ({"ending": "fixed", "proposal": {"pr": 14589}}, None, "pr-open"),
    ({"ending": "sandbox", "fault": True}, None, "failed"),
    ({"ending": "no-fix"}, None, "declined"),
    ({"ending": "fixed"}, {"action": "propose", "status": "failed", "source": "operator",
                           "reason": "gh said no"}, "failed"),
])
def test_the_status_reads_whose_move_it_is(store, run, req, want):
    _run(store, **run)
    if req:
        store.edit_issue(7).record_fix_request(req)
    assert fix_review.fix_status(store.load_issue(7))[0] == want


def test_a_claim_is_taken_once_and_only_by_the_named_host(store):
    fix_review.queue(store, 7, "solve", by="op")
    assert store.claim_fix_request(7, host="laptop", hosts=frozenset({"studio"})) is None
    claimed = store.claim_fix_request(7, host="studio")
    assert claimed["status"] == "running" and claimed["host"] == "studio"
    assert store.claim_fix_request(7, host="other") is None


def test_a_queued_request_can_be_cancelled_and_a_running_one_cannot(store):
    fix_review.queue(store, 7, "solve", by="op")
    assert fix_review.cancel(store, 7, by="op") == (True, "cancelled")
    fix_review.queue(store, 7, "solve", by="op")
    store.claim_fix_request(7, host="studio")
    assert not fix_review.cancel(store, 7, by="op")[0]


def test_the_distilled_run_carries_each_candidate_s_own_account(monkeypatch):
    monkeypatch.setattr(settings, "worker_id", lambda: "studio")
    record = {"lane": "cross", "ending": "fixed", "detail": "d", "base_sha": "a" * 40,
              "report_sha": "r", "models": ["opus", "sonnet"], "agent_runs": 4,
              "result": {
                  "patch": "diff --git a/src/x.ts b/src/x.ts\n+x\n"
                           "diff --git a/src/x.test.ts b/src/x.test.ts\n+t\n" + "y" * 70_000,
                  "summary": "Fix x", "root_cause": "rc", "pick": 1,
                  "candidates": [{"index": 1, "model": "sonnet", "ending": None,
                                  "reproduces": True, "passes": {"1": True}, "fix_lines": 3}],
                  "candidate_patches": [{"index": 1, "verdict": {
                      "summary": "Fix x", "root_cause": "rc",
                      "changes": [{"path": "src/x.ts", "rationale": "why"}]}}],
                  "reviews": [{"lens": "scope-safety", "verdict": "safe", "reason": "fine",
                               "unasked": ["empty input"]}],
                  "proof": {"compile": {"exit": 0}, "suite": {"excluded": 3, "confirmed": False}}}}
    run = fix_review.distill(record, QUESTION)
    assert run["host"] == "studio" and run["patch_truncated"]
    assert len(run["patch"]) == fix_review.PATCH_MAX
    assert run["tests"] == ["src/x.test.ts"]
    assert run["candidates"][0]["summary"] == "Fix x"
    assert run["candidates"][0]["changes"][0]["rationale"] == "why"
    assert run["reviews"][0]["unasked"] == ["empty input"]
    assert run["proof"]["suite"]["excluded"] == 3
    assert run["question"]["labels"] == ["A", "B"]


# --- the runner --------------------------------------------------------------------

def test_a_solve_records_the_distilled_run_and_ends_the_request(store, monkeypatch):
    monkeypatch.setattr(settings, "worker_id", lambda: "studio")
    record = {"ending": "fixed", "detail": "proven", "result": {"patch": "diff --git a/x b/x\n"}}
    monkeypatch.setattr(fix_review_runner, "solve",
                        lambda s, n, **kw: (record, None))
    fix_review.queue(store, 7, "solve", by="op")
    req = store.claim_fix_request(7, host="studio")
    status, outcome = fix_review_runner.run_request(store, 7, req)
    issue = store.load_issue(7)
    assert status == "done" and "Solved: fixed" in outcome
    assert issue.fix_request["status"] == "done"
    assert issue.fix_run["ending"] == "fixed" and issue.fix_run["host"] == "studio"
    assert issue.fix_thread[-1]["by"] == "worker"
    assert fix_review.fix_status(issue)[0] == "review"


@pytest.mark.parametrize("mode,want", [("revise", "Revised"), ("restart", "Started over")])
def test_a_send_back_follows_what_the_comments_ask(store, monkeypatch, mode, want):
    _run(store)
    record = {"ending": "fixed", "detail": "d", "result": {"patch": "diff --git a/x b/x\n"}}
    monkeypatch.setattr(fix_review_runner, "route", lambda comments: mode)
    monkeypatch.setattr(fix_review_runner, "revise", lambda s, n, **kw: record)
    monkeypatch.setattr(fix_review_runner, "solve", lambda s, n, **kw: (record, None))
    fix_review.queue(store, 7, "send-back", by="op", guidance="handle the empty case too")
    req = store.claim_fix_request(7, host="studio")
    status, outcome = fix_review_runner.run_request(store, 7, req)
    assert status == "done" and outcome.startswith(want)


def test_a_failure_ends_the_request_failed_with_the_reason(store, monkeypatch):
    def boom(s, n, **kw):
        raise fix_review_runner.RequestFailed("the base this attempt was proven on is gone")

    monkeypatch.setattr(fix_review_runner, "solve", boom)
    fix_review.queue(store, 7, "solve", by="op")
    req = store.claim_fix_request(7, host="studio")
    status, outcome = fix_review_runner.run_request(store, 7, req)
    issue = store.load_issue(7)
    assert status == "failed" and "base" in outcome
    assert fix_review.fix_status(issue) == ("failed", issue.fix_request["reason"])


def test_a_proposal_that_opens_records_the_pull_request(store, monkeypatch):
    from prospector_app.backend import executor
    _run(store)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(executor, "propose_issue_fix", lambda n, **kw: {
        "status": "executed", "detail": "opened #9", "pr": 9, "url": "u"})
    fix_review.queue(store, 7, "propose", by="op")
    req = store.claim_fix_request(7, host="studio")
    status, _ = fix_review_runner.run_request(store, 7, req)
    assert status == "done"
    assert store.load_issue(7).fix_run["proposal"] == {"pr": 9, "url": "u"}
    assert fix_review.fix_status(store.load_issue(7))[0] == "pr-open"


def test_a_dry_run_proposal_opens_nothing(store, monkeypatch):
    from prospector_app.backend import executor
    _run(store)
    seen = {}
    monkeypatch.setattr(executor, "propose_issue_fix", lambda n, **kw: seen.update(kw) or {
        "status": "dry-run", "detail": "would open it"})
    fix_review.queue(store, 7, "propose", by="op", dry_run=True)
    req = store.claim_fix_request(7, host="studio")
    _, outcome = fix_review_runner.run_request(store, 7, req)
    assert seen == {"token": None, "dry_run": True} and outcome == "Dry run: would open it"
    assert fix_review.fix_status(store.load_issue(7))[0] == "review"
