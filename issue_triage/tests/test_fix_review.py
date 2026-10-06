"""Reviewing issue fixes in the app: the queue's rules, the claim, the derived
status, the distilled run, and the runner's dispatch."""
from __future__ import annotations

import json

import pytest

from issue_triage import fix_review, fix_review_runner, propose
from issue_triage.issue_store import IssueStore
from pipeline import settings
from pipeline.storekit import ValidationError

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
    ("solve", {}, {"proposal": {"pr": 9}}, "#9 is open"),
])
def test_an_action_that_does_not_fit_the_attempt_is_refused(store, action, kw, run, why):
    _run(store, **run)
    ok, reason = fix_review.queue(store, 7, action, by="op", **kw)
    assert not ok and why in reason


def test_a_proposal_the_follow_up_saw_close_no_longer_holds_the_attempt(store):
    _run(store, proposal={"pr": 9, "url": "u"})
    assert fix_review.open_pr(store.load_issue(7)) == 9
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "done"})
    assert fix_review.open_pr(store.load_issue(7)) is None
    assert fix_review.queue(store, 7, "solve", by="op")[0]


def test_an_answer_names_an_option_or_says_what_should_happen(store):
    _run(store, ending="fix-disputed", question=QUESTION)
    assert fix_review.queue(store, 7, "answer", by="op", answer={"label": "B"})[0]
    assert store.load_issue(7).fix_thread[-1]["text"] == "Answered B"
    fix_review.cancel(store, 7, by="op")
    assert fix_review.queue(store, 7, "answer", by="op", answer={"text": "x reads 2"})[0]
    fix_review.cancel(store, 7, by="op")
    assert fix_review.queue(store, 7, "answer", by="reporter", source="public",
                            notes="reporter:\n> x reads 2")[0]
    assert "answer" not in store.load_issue(7).fix_request


def test_notes_alone_send_a_fix_back_and_join_the_thread(store):
    _run(store)
    assert fix_review.queue(store, 7, "send-back", by="followup", source="followup",
                            notes="CI fails in ui")[0]
    issue = store.load_issue(7)
    assert "guidance" not in issue.fix_request and issue.fix_request["notes"] == "CI fails in ui"
    assert issue.fix_thread[-1]["text"] == "CI fails in ui"


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


@pytest.mark.parametrize("fu,want", [
    ({"pr": 14589, "state": "done", "closed_as": "closed", "reason": "#14589 is closed"},
     ("pr-closed", "#14589 was closed without merging")),
    ({"pr": 14589, "state": "done", "closed_as": "merged", "reason": "#14589 is merged"},
     ("pr-merged", "#14589 merged")),
    ({"pr": 14589, "state": "done", "reason": "#14589 is closed"},
     ("pr-closed", "#14589 is closed")),
    ({"pr": 14000, "state": "done", "closed_as": "closed"}, ("pr-open", "#14589")),
])
def test_a_proposal_s_status_reads_its_own_pull_request_s_follow_up(store, fu, want):
    _run(store, proposal={"pr": 14589})
    store.edit_issue(7).record_fix_followup(fu)
    status = fix_review.fix_status(store.load_issue(7))
    assert status == want and status[0] in fix_review.STATUSES


def test_a_new_proposal_is_open_past_an_earlier_one_s_finished_follow_up(store):
    _run(store, proposal={"pr": 15000})
    store.edit_issue(7).record_fix_followup({"pr": 14589, "state": "done", "closed_as": "closed"})
    assert fix_review.open_pr(store.load_issue(7)) == 15000
    store.edit_issue(7).record_fix_followup({"pr": 15000, "state": "done", "closed_as": "closed"})
    assert fix_review.open_pr(store.load_issue(7)) is None


def test_a_proposal_closed_without_merging_takes_a_new_attempt(store):
    _run(store, proposal={"pr": 14589, "url": "u"})
    store.edit_issue(7).record_fix_followup({"pr": 14589, "state": "done", "closed_as": "closed",
                                             "reason": "#14589 is closed"})
    ok, why = fix_review.queue(store, 7, "propose", by="op")
    assert not ok and why == "#14589 was closed without merging; try again for a new change"
    assert fix_review.queue(store, 7, "solve", by="op", guidance="keep the old API") == (
        True, "solve queued")


def test_a_merged_proposal_is_not_proposed_again(store):
    _run(store, proposal={"pr": 14589})
    store.edit_issue(7).record_fix_followup({"pr": 14589, "state": "done", "closed_as": "merged"})
    ok, why = fix_review.queue(store, 7, "propose", by="op")
    assert not ok and why == "this change already merged as #14589"


def test_a_follow_up_names_how_its_pull_request_ended(store):
    with pytest.raises(ValidationError, match="closed_as"):
        store.edit_issue(7).record_fix_followup({"pr": 9, "state": "done", "closed_as": "gone"})


def test_a_claim_is_taken_once_and_only_by_the_named_host(store):
    fix_review.queue(store, 7, "solve", by="op")
    assert store.claim_fix_request(7, host="laptop", hosts=frozenset({"studio"})) is None
    claimed = store.claim_fix_request(7, host="studio")
    assert claimed["status"] == "running" and claimed["host"] == "studio"
    assert store.claim_fix_request(7, host="other") is None


def test_a_retry_of_a_failed_request_counts_its_attempt(store):
    fix_review.queue(store, 7, "solve", by="op")
    assert store.load_issue(7).fix_request["attempts"] == 1
    store.claim_fix_request(7, host="studio")
    issue = store.edit_issue(7)
    issue.record_fix_request({**issue.fix_request, "status": "failed"})
    fix_review.queue(store, 7, "solve", by="op")
    assert store.load_issue(7).fix_request["attempts"] == 2
    fix_review.cancel(store, 7, by="op")
    fix_review.queue(store, 7, "solve", by="op")
    assert store.load_issue(7).fix_request["attempts"] == 1


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
                  "proof": {"compile": {"exit": 0}, "suite": {"excluded": 3, "confirmed": False},
                            "lint": {"exit": 20, "error": "fails on trunk itself",
                                     "error_kind": "base-lint", "error_excerpt": "raw hex"}}}}
    run = fix_review.distill(record, QUESTION)
    assert run["proof"]["lint"] == {"exit": 20, "base_fails": True, "excerpt": "raw hex"}
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


ASKED = f"The fix attempt asked: {QUESTION['question']}\n"


@pytest.mark.parametrize("words,guidance,notes", [
    ({"answer": {"text": "x reads 2"}}, ASKED + "A maintainer answered: x reads 2", None),
    ({"notes": "reporter:\n> x reads 2"}, None,
     ASKED + "The issue's author answered:\nreporter:\n> x reads 2"),
])
def test_a_written_answer_keeps_who_wrote_it(store, tmp_path, monkeypatch, words, guidance,
                                             notes):
    _run(store, ending="fix-disputed", question=QUESTION)
    monkeypatch.setattr(propose, "result_dir", lambda n: tmp_path / f"issue-{n}")
    (tmp_path / "issue-7").mkdir()
    (tmp_path / "issue-7" / "result.json").write_text(json.dumps({"ending": "fix-disputed"}))
    (tmp_path / "issue-7" / "question.json").write_text(json.dumps({"question": QUESTION}))
    seen = {}
    monkeypatch.setattr(fix_review_runner, "solve", lambda s, n, **kw: seen.update(kw) or (
        {"ending": "no-fix", "detail": "d", "result": {}}, None))
    fix_review.queue(store, 7, "answer", by="op", **words)
    fix_review_runner.run_request(store, 7, store.claim_fix_request(7, host="s"))
    assert (seen["guidance"], seen["notes"]) == (guidance, notes)


def _taken_back(store: IssueStore, req: dict) -> None:
    store.edit_issue(7).record_fix_request(
        {**req, "status": "failed", "reason": "interrupted: the issue-fix worker on laptop "
                                              "went offline mid-solve"})


def test_a_run_taken_back_still_records_its_ending(store, monkeypatch):
    record = {"ending": "fixed", "detail": "proven", "result": {"patch": "diff --git a/x b/x\n"}}
    monkeypatch.setattr(fix_review_runner, "solve", lambda s, n, **kw: (record, None))
    fix_review.queue(store, 7, "solve", by="op")
    req = store.claim_fix_request(7, host="laptop")
    _taken_back(store, req)
    fix_review_runner.run_request(store, 7, req)
    assert store.load_issue(7).fix_request["status"] == "done"


def test_a_run_taken_back_leaves_the_request_queued_since(store, monkeypatch):
    record = {"ending": "fixed", "detail": "proven", "result": {"patch": "diff --git a/x b/x\n"}}
    monkeypatch.setattr(fix_review_runner, "solve", lambda s, n, **kw: (record, None))
    fix_review.queue(store, 7, "solve", by="op")
    req = store.claim_fix_request(7, host="laptop")
    _taken_back(store, req)
    fix_review.queue(store, 7, "solve", by="op", guidance="try the parser")
    fix_review_runner.run_request(store, 7, req)
    after = store.load_issue(7).fix_request
    assert after["status"] == "queued" and after["guidance"] == "try the parser"


@pytest.fixture
def proposed(store, tmp_path, monkeypatch):
    """Issue 7's fixed attempt open as #9, handed back, with its result on disk."""
    _run(store, proposal={"pr": 9, "url": "u"})
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "handed-back", "head_sha": "h" * 40,
                                             "reason": "2 revisions spent", "revisions": 2})
    monkeypatch.setattr(propose, "result_dir", lambda n: tmp_path / f"issue-{n}")
    (tmp_path / "issue-7").mkdir()
    (tmp_path / "issue-7" / "result.json").write_text(json.dumps(
        {"ending": "fixed", "result": {"patch": "diff --git a/x b/x\n"}}))
    monkeypatch.setattr(fix_review_runner, "route", lambda comments: "revise")
    monkeypatch.setattr(fix_review_runner, "solve",
                        lambda *a, **k: pytest.fail("started over on an open pull request"))
    return tmp_path / "issue-7" / "result.json"


def _revised(result_file, ending: str = "fixed"):
    def revise(store_, n, **kw):
        result_file.write_text(json.dumps({"ending": ending}))
        return {"ending": ending, "detail": "d", "result": {"patch": "diff --git a/x b/x\n+y\n"}}
    return revise


def test_an_operator_send_back_on_an_open_pull_request_goes_onto_it(store, proposed, monkeypatch):
    from prospector_app.backend import executor
    monkeypatch.setattr(fix_review_runner, "revise", _revised(proposed))
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "t")
    calls = []
    monkeypatch.setattr(executor, "update_issue_fix_proposal",
                        lambda n, pr, **kw: calls.append((n, pr, kw)) or {
                            "status": "executed", "detail": "Pushed a revision on #9"})
    fix_review.queue(store, 7, "send-back", by="op", guidance="handle the empty case too")
    status, outcome = fix_review_runner.run_request(store, 7, store.claim_fix_request(7, host="s"))
    issue = store.load_issue(7)
    assert status == "done" and outcome.startswith("Revised #9")
    assert calls == [(7, 9, {"push": True, "token": "t", "dry_run": False})]
    assert issue.fix_run["proposal"] == {"pr": 9, "url": "u"}
    assert issue.fix_run["patch"].endswith("+y\n")
    assert issue.fix_followup["state"] == "watching" and issue.fix_followup["revisions"] == 2
    assert fix_review.fix_status(issue)[0] == "pr-open"


def test_a_send_back_that_asks_to_start_over_an_open_pull_request_is_refused(
        store, proposed, monkeypatch):
    kept = proposed.read_text()
    monkeypatch.setattr(fix_review_runner, "route", lambda comments: "restart")
    fix_review.queue(store, 7, "send-back", by="op", guidance="wrong approach, start over")
    status, outcome = fix_review_runner.run_request(store, 7, store.claim_fix_request(7, host="s"))
    issue = store.load_issue(7)
    assert status == "failed" and "#9 is open" in outcome
    assert issue.fix_run["proposal"] == {"pr": 9, "url": "u"}
    assert proposed.read_text() == kept


@pytest.mark.parametrize("ending,res,want", [
    ("fixed", {"status": "dry-run", "detail": "would have pushed a revision on #9"}, "Dry run:"),
    ("no-fix", None, "#9 is unchanged"),
])
def test_a_send_back_that_does_not_reach_the_pull_request_leaves_it_as_it_was(
        store, proposed, monkeypatch, ending, res, want):
    from prospector_app.backend import executor
    kept = proposed.read_text()
    monkeypatch.setattr(fix_review_runner, "revise", _revised(proposed, ending))
    monkeypatch.setattr(executor, "mint_bot_token", lambda: pytest.fail("minted on a dry run"))
    seen = {}
    monkeypatch.setattr(executor, "update_issue_fix_proposal",
                        lambda n, pr, **kw: seen.update(kw) or res)
    fix_review.queue(store, 7, "send-back", by="op", guidance="g", dry_run=True)
    status, outcome = fix_review_runner.run_request(store, 7, store.claim_fix_request(7, host="s"))
    issue = store.load_issue(7)
    assert status == "done" and want in outcome
    assert seen == ({"push": True, "token": None, "dry_run": True} if res else {})
    assert proposed.read_text() == kept
    assert issue.fix_run["proposal"] == {"pr": 9, "url": "u"} and issue.fix_run["patch"] == (
        "diff --git a/x b/x\n")
    assert issue.fix_followup["state"] == "handed-back"
    assert fix_review.fix_status(issue)[0] == "review"


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


def test_a_request_runs_whatever_other_pull_requests_name_the_issue(store, monkeypatch):
    from issue_triage import related_prs
    record = {"ending": "fixed", "detail": "proven", "result": {"patch": "diff --git a/x b/x\n"}}
    monkeypatch.setattr(fix_review_runner, "solve", lambda s, n, **kw: (record, None))
    monkeypatch.setattr(related_prs, "search", lambda issue, exclude=None: [
        {"number": 14918, "title": "fix x", "state": "open", "author": "contrib",
         "closes": True}])
    fix_review.queue(store, 7, "solve", by="hunter", source="hunter")
    status, _ = fix_review_runner.run_request(store, 7, store.claim_fix_request(7, host="s"))
    assert status == "done"
    assert fix_review.fix_status(store.load_issue(7))[0] == "review"


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


def test_a_new_proposal_after_a_closed_one_reads_open(store, monkeypatch):
    from prospector_app.backend import executor
    _run(store)
    store.edit_issue(7).record_fix_followup({"pr": 14589, "state": "done", "closed_as": "closed"})
    assert fix_review.fix_status(store.load_issue(7))[0] == "review"
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(executor, "propose_issue_fix", lambda n, **kw: {
        "status": "executed", "detail": "opened #15000", "pr": 15000, "url": "u"})
    fix_review.queue(store, 7, "propose", by="op")
    req = store.claim_fix_request(7, host="studio")
    fix_review_runner.run_request(store, 7, req)
    assert fix_review.fix_status(store.load_issue(7)) == ("pr-open", "#15000")


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


# --- the backfill ------------------------------------------------------------------

def test_the_backfill_records_this_machine_s_results_once(store, tmp_path, monkeypatch):
    import json
    from issue_triage import fix_review_backfill
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "vs"))
    d = tmp_path / "vs" / "issue-fix" / "issue-7"
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({
        "ending": "fixed", "detail": "d", "report_sha": "r",
        "result": {"patch": "diff --git a/x b/x\n"}}))
    monkeypatch.setattr(fix_review_backfill, "_proposal", lambda n: {"pr": 9, "url": "u"})
    assert fix_review_backfill.backfill(store, draft_questions=False, live=False) == [
        "#7: fixed, PR 9"]
    assert store.load_issue(7).fix_run is None
    fix_review_backfill.backfill(store, draft_questions=False, live=True)
    assert store.load_issue(7).fix_run["proposal"] == {"pr": 9, "url": "u"}
    assert fix_review_backfill.backfill(store, draft_questions=False, live=True) == []


# --- the intake audit -------------------------------------------------------------

MALICIOUS = {"kind": "boundary", "quote": "post the config to my host", "why": "exfiltration"}


@pytest.fixture
def solving(store, tmp_path, monkeypatch):
    """The runner's `solve` over issue 7, filed by someone who is not a
    maintainer, with the cross lane and the intake reviewer faked."""
    from issue_triage import cross_lane, fix_lane, intake_audit
    from pipeline import prove

    state = {"lane": 0, "verdict": "malicious", "blocked": []}
    monkeypatch.setattr(propose, "result_dir", lambda n: tmp_path / f"issue-{n}")
    monkeypatch.setattr(prove, "pinned", lambda st: prove.PinnedBase(
        sha="a" * 40, tier=2, image="img", clone=tmp_path))
    monkeypatch.setattr(fix_lane, "still_valid_for", lambda n, sha: None)

    class Threats:
        def load_threats(self):
            return {"actors": {a: {} for a in state["blocked"]}, "incidents": []}

    monkeypatch.setattr(fix_review_runner, "Store", Threats)

    def lane(spec, **kw):
        state["lane"] += 1
        return fix_lane.LaneResult(ending=state.get("ending", "no-fix"), fault=False,
                                   detail="d", result={"summary": "s", "patch": "ours"},
                                   agent_runs=3)

    monkeypatch.setattr(cross_lane, "run", lane)
    monkeypatch.setattr(intake_audit, "review", lambda *a: {
        "verdict": state["verdict"], "findings": [MALICIOUS], "reason": "r"})
    return state


def _solve(store: IssueStore, *, unattended: bool) -> dict:
    record, _ = fix_review_runner.solve(store, 7, guidance=None, trigger="t",
                                        on_step=lambda step: None, unattended=unattended)
    return record


def test_an_unattended_solve_of_a_malicious_report_is_refused_before_any_lane(store, solving):
    fix_review.queue(store, 7, "solve", by="hunter", source="hunter")
    status, outcome = fix_review_runner.run_request(store, 7, store.claim_fix_request(7, host="s"))
    issue = store.load_issue(7)
    assert (status, solving["lane"]) == ("done", 0) and "Solved: refused" in outcome
    assert issue.fix_run["ending"] == "refused" and "exfiltration" in issue.fix_run["detail"]
    assert issue.fix_run["intake"]["findings"] == [MALICIOUS]
    assert fix_review.fix_status(issue)[0] == "declined"


def test_an_operator_s_solve_runs_and_carries_the_audit(store, solving):
    record = _solve(store, unattended=False)
    assert solving["lane"] == 1 and record["agent_runs"] == 4
    assert record["result"]["intake"]["verdict"] == "malicious"


def test_a_blocked_author_is_refused_without_a_review(store, solving):
    solving["blocked"] = ["reporter"]
    solving["verdict"] = "clear"
    record = _solve(store, unattended=True)
    assert (record["ending"], record["agent_runs"]) == ("refused", 0)
    assert record["result"]["intake"]["blocked"] is True


def test_a_maintainer_s_issue_is_taken_as_written(store, solving):
    raw = store.load_issue(7).raw
    store.save_issue({**raw, "meta": {**raw["meta"], "author_association": "MEMBER"}})
    record = _solve(store, unattended=True)
    assert solving["lane"] == 1 and "intake" not in record["result"]


def test_a_revision_keeps_the_attempt_s_intake(store, solving, tmp_path, monkeypatch):
    from issue_triage import fix_lane, solo_lane
    intake = {"verdict": "suspicious", "findings": [MALICIOUS], "reason": "r"}
    (tmp_path / "issue-7").mkdir()
    (tmp_path / "issue-7" / "result.json").write_text(json.dumps(
        {"ending": "fixed", "base_sha": "a" * 40, "base_tier": 2,
         "result": {"patch": "diff --git a/x b/x\n", "intake": intake}}))
    monkeypatch.setattr(solo_lane, "run", lambda spec, **kw: fix_lane.LaneResult(
        ending="fixed", fault=False, detail="d", result={"patch": "p"}))
    record = fix_review_runner.revise(store, 7, comments="handle empty", on_step=lambda s: None)
    assert record["result"]["intake"] == intake


# --- the second opinion ------------------------------------------------------------

GAP = {"pr": 12, "author": "contrib", "verdict": "gap", "why": "our fix fails x.test.ts",
       "output": "FAIL x.test.ts > empty"}


@pytest.fixture
def rival_gap(store, solving, tmp_path, monkeypatch):
    """Issue 7's cross lane ends fixed and one rival's tests fail with it; the
    revision and the recheck are faked."""
    from issue_triage import second_opinion
    rival = second_opinion.Rival(pr=12, author="contrib", title="t", tests="T", fix="F",
                                 test_paths=["x.test.ts"])
    solving.update(ending="fixed", verdict="clear", revised="fixed", revisions=[])
    monkeypatch.setattr(second_opinion, "rivals", lambda n, **kw: ([rival], [
        {"pr": 13, "author": "x", "verdict": "skipped", "why": "it adds no tests"}]))
    monkeypatch.setattr(second_opinion, "judge", lambda base, r, patch, **kw: dict(GAP))
    monkeypatch.setattr(second_opinion, "recheck", lambda base, r, patch, entry, **kw: {
        **entry, "verdict": "gap-closed" if patch == "revised" else "gap-open", "why": "w"})

    def revise(store_, n, **kw):
        solving["revisions"].append(kw)
        return {"ending": solving["revised"], "detail": "d",
                "result": {"patch": "revised", "summary": "s2"}}

    monkeypatch.setattr(fix_review_runner, "revise", revise)
    return solving


def test_a_rival_s_failing_test_sends_the_fix_back_once_and_credits_its_author(store,
                                                                               rival_gap):
    from issue_triage import second_opinion
    record = _solve(store, unattended=True)
    [kw] = rival_gap["revisions"]
    assert kw["comments"] == "" and "FAIL x.test.ts > empty" in kw["notes"]
    assert record["result"]["patch"] == "revised"
    entries = record["result"]["second_opinion"]
    assert [e["verdict"] for e in entries] == ["skipped", "gap-closed"]
    assert second_opinion.credit(entries) == [{"pr": 12, "author": "contrib"}]
    on_disk = json.loads((propose.result_dir(7) / "result.json").read_text())
    assert on_disk["result"]["second_opinion"] == entries


def test_a_revision_that_does_not_end_fixed_keeps_the_fix_and_holds_it(store, rival_gap):
    from issue_triage import trust_boundary
    rival_gap["revised"] = "no-fix"
    record = _solve(store, unattended=True)
    assert record["result"]["patch"] == "ours"
    assert [e["verdict"] for e in record["result"]["second_opinion"]] == ["skipped", "gap-open"]
    fix_review_runner._write_run(store, 7, record, None)
    assert "#12" in (trust_boundary.held(store.load_issue(7)) or "")
