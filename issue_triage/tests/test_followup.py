"""Following up a proposed pull request: the step each state calls for, the
evidence a revision is handed, and a dry-run poll that writes nothing upstream."""
from __future__ import annotations

import json

import pytest

from issue_triage import fix_review, fix_review_runner, followup, propose
from issue_triage.followup import Check, PrState, ReviewerView
from issue_triage.issue_store import IssueStore
from pipeline import reviewers

HEAD = "h" * 40
PATCH = """diff --git a/ui/src/EmailMessageCard.tsx b/ui/src/EmailMessageCard.tsx
--- a/ui/src/EmailMessageCard.tsx
+++ b/ui/src/EmailMessageCard.tsx
@@ -1 +1 @@
-a
+b
"""


def _check(name: str = "tests", conclusion: str | None = "success", run_id: int = 1,
           status: str = "completed") -> Check:
    return Check(name=name, status=status, conclusion=conclusion, run_id=run_id, job_id=run_id * 10)


def _greptile(status: str = reviewers.PASS, **over) -> ReviewerView:
    return ReviewerView(id="greptile", label="Greptile", status=status,
                        reason=None if status == reviewers.PASS else "greptile 4/5", **over)


def _pr(checks: list[Check] | None = None, views: list[ReviewerView] | None = None,
        state: str = "open") -> PrState:
    return PrState(number=9, state=state, head_sha=HEAD,
                   checks=checks if checks is not None else [_check()],
                   reviewers=views if views is not None else [_greptile()])


DESCRIBED = {"described_head": HEAD, "state": "watching"}


def _decide(pr: PrState, fu: dict | None = None, **kw) -> followup.Step:
    return followup.decide(pr, DESCRIBED if fu is None else fu,
                           older_open=kw.pop("older_open", []), patch=PATCH, **kw)


def test_a_merged_or_closed_pull_request_ends_the_follow_up():
    assert _decide(_pr(state="merged")).kind == "done"


def test_an_older_open_pull_request_on_the_issue_hands_it_back():
    step = _decide(_pr(), older_open=[5])
    assert step.kind == "hand-back" and "#5" in step.reason


def test_each_head_gets_its_description_re_rendered_once():
    assert _decide(_pr(), {"state": "watching"}).kind == "describe"


def test_a_green_pull_request_with_passing_reviewers_is_ready():
    assert _decide(_pr()).kind == "ready"


def test_failing_ci_is_re_run_once_per_head():
    pr = _pr([_check("chat", "failure", run_id=4), _check("unit", "failure", run_id=5)])
    step = _decide(pr)
    assert step.kind == "rerun" and step.run_ids == [4, 5]
    after = _decide(pr, {**DESCRIBED, "reruns": {HEAD: [4, 5]}})
    assert after.kind == "wait" and after.jobs == [40, 50]


def test_ci_still_failing_with_logs_that_never_name_the_change_is_handed_back():
    pr = _pr([_check("chat", "failure", run_id=4)])
    step = _decide(pr, {**DESCRIBED, "reruns": {HEAD: [4]}},
                   logs={"job 40": "FAIL server/chat.test.ts > discord"})
    assert step.kind == "hand-back" and "no failing log names" in step.reason


def test_ci_still_failing_in_a_log_that_names_a_changed_file_is_revised():
    pr = _pr([_check("ui", "failure", run_id=4)])
    step = _decide(pr, {**DESCRIBED, "reruns": {HEAD: [4]}},
                   logs={"job 40": "FAIL ui/src/EmailMessageCard.test.tsx"})
    assert step.kind == "revise"
    assert "EmailMessageCard" in (step.guidance or "") and "quoted evidence" in (step.guidance or "")


def test_a_reviewer_below_its_bar_sends_the_fix_back_with_its_findings():
    finding = {"path": "ui/a.tsx", "line": 32, "title": "Cached email details disappear",
               "body": "When connectors are off the provider supplies null."}
    step = _decide(_pr(views=[_greptile(reviewers.FAIL, findings=[finding],
                                        summary="Medium risk.")]))
    assert step.kind == "revise"
    assert "ui/a.tsx:32" in (step.guidance or "") and "Medium risk." in (step.guidance or "")


def test_the_revision_budget_hands_it_back():
    step = _decide(_pr(views=[_greptile(reviewers.FAIL)]),
                   {**DESCRIBED, "revisions": followup.MAX_REVISIONS})
    assert step.kind == "hand-back" and "revisions spent" in step.reason


def test_pending_ci_or_a_stale_reviewer_waits():
    assert _decide(_pr([_check(status="in_progress", conclusion=None)])).kind == "wait"
    assert _decide(_pr(views=[_greptile(reviewers.STALE)])).kind == "wait"


def test_a_hand_back_holds_until_the_head_moves():
    held = {**DESCRIBED, "state": "handed-back", "head_sha": HEAD}
    assert _decide(_pr(views=[_greptile(reviewers.FAIL)]), held).kind == "wait"
    moved = {**held, "head_sha": "g" * 40}
    assert _decide(_pr(views=[_greptile(reviewers.FAIL)]), moved).kind == "revise"


# --- the poll ---------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    s = IssueStore(tmp_path / "store")
    s.save_issue({"issue": 7, "meta": {"title": "x", "state": "open",
                                       "updated_at": "2026-09-01T00:00:00Z"}})
    s.edit_issue(7).record_fix_run({"ending": "fixed", "detail": "ok", "patch": PATCH,
                                    "host": "studio", "question": None,
                                    "proposal": {"pr": 9, "url": "u"}})
    monkeypatch.setattr(propose, "result_dir", lambda n: tmp_path / f"issue-{n}")
    (tmp_path / "issue-7").mkdir()
    (tmp_path / "issue-7" / "result.json").write_text(json.dumps(
        {"ending": "fixed", "result": {"patch": PATCH}}))
    monkeypatch.setattr(followup, "_older_open", lambda issue, pr: [])
    return s


def test_a_dry_run_poll_notes_each_step_and_writes_nothing(store, monkeypatch):
    from prospector_app.backend import executor
    monkeypatch.setattr(followup, "read", lambda pr: _pr(views=[_greptile(reviewers.FAIL)]))
    monkeypatch.setattr(executor, "update_issue_fix_proposal",
                        lambda *a, **k: pytest.fail("wrote upstream"))
    assert followup.poll(store, mode="dry-run") == 1
    issue = store.load_issue(7)
    assert issue.fix_followup["described_head"] == HEAD
    assert "Dry run: would describe" in issue.fix_thread[-1]["text"]
    followup.poll(store, mode="dry-run")
    issue = store.load_issue(7)
    assert "Dry run: would revise" in issue.fix_thread[-1]["text"]
    assert issue.fix_followup["state"] == "handed-back"
    assert issue.fix_request is None
    assert fix_review.fix_status(issue)[0] == "review"


def test_a_live_poll_queues_a_follow_up_revision(store, monkeypatch):
    monkeypatch.setattr(followup, "read", lambda pr: _pr(views=[_greptile(reviewers.FAIL)]))
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "watching",
                                             "described_head": HEAD})
    followup.poll(store, mode="live")
    req = store.load_issue(7).fix_request
    assert req["action"] == "send-back" and req["source"] == "followup"
    assert store.load_issue(7).fix_followup["revisions"] == 1


def test_a_failed_follow_up_revision_leaves_the_proposal_as_it_was(store, monkeypatch, tmp_path):
    kept = (tmp_path / "issue-7" / "result.json").read_text()

    def failing_revise(store_, n, **kw):
        (tmp_path / "issue-7" / "result.json").write_text('{"ending": "no-fix"}')
        return {"ending": "no-fix", "detail": "nothing to change"}

    monkeypatch.setattr(fix_review_runner, "revise", failing_revise)
    out = fix_review_runner._follow_up(store, 7, "g", on_step=lambda s: None)
    assert "#9 is unchanged" in out
    assert (tmp_path / "issue-7" / "result.json").read_text() == kept
    assert store.load_issue(7).fix_run["proposal"] == {"pr": 9, "url": "u"}


def test_a_fixed_follow_up_revision_goes_onto_the_same_pull_request(store, monkeypatch):
    from prospector_app.backend import executor
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_FOLLOWUP", "live")
    monkeypatch.setattr(fix_review_runner, "revise", lambda *a, **k: {
        "ending": "fixed", "detail": "ok", "result": {"patch": PATCH}})
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "t")
    calls = []
    monkeypatch.setattr(executor, "update_issue_fix_proposal",
                        lambda n, pr, **kw: calls.append((n, pr, kw)) or {
                            "status": "executed", "detail": "Pushed a revision on #9"})
    out = fix_review_runner._follow_up(store, 7, "g", on_step=lambda s: None)
    assert calls == [(7, 9, {"push": True, "token": "t", "dry_run": False})]
    assert out.startswith("Revised #9")
    assert store.load_issue(7).fix_run["proposal"] == {"pr": 9, "url": "u"}


def test_a_reviewer_that_reviewed_the_pull_request_gates_it(monkeypatch):
    from pipeline import review_fetch, review_policy
    monkeypatch.setattr(review_policy, "active_reviewers", lambda kind=None: [])
    feed = review_fetch.PrFeed(pr=9, head_sha=HEAD, updated_at=None, comments=[
        {"id": 1, "login": "greptile-apps[bot]", "body": "Confidence Score: 4/5",
         "at": "t", "updated_at": "t", "url": "u"}])
    assert [r.id for r in followup._gating(feed, HEAD)] == ["greptile"]
    assert followup._gating(None, HEAD) == []


def test_review_evidence_keeps_suggested_code_and_drops_badges():
    finding = {"path": "a.tsx", "line": 1, "body": '<a href="#"><img alt="P1" src="x"></a> '
               "Fix it. ```suggestion <Ctx.Provider value={null}> ```"}
    step = _decide(_pr(views=[_greptile(reviewers.FAIL, findings=[finding])]))
    assert "<Ctx.Provider value={null}>" in (step.guidance or "")
    assert "<img" not in (step.guidance or "")
