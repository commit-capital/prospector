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
    return followup.decide(pr, DESCRIBED if fu is None else fu, **kw)


def test_a_merged_or_closed_pull_request_ends_the_follow_up():
    assert _decide(_pr(state="merged")).kind == "done"
    step = _decide(_pr(state="closed"))
    assert step.kind == "done" and step.reason == "#9 was closed without merging"


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


# A fix pull request's failing job log excerpt (issue #16267's fix): the fix exported a
# new function from services/issues.ts, and a route test that vi.doMock()s that
# module without it got a 500 from the route.
LOG_16929 = """\
     × uses the company-scope fast path on the issue list route 33ms
      "type": "TypeError",
          TypeError: instanceSettings.getExperimental is not a function
 FAIL   @acme/server  src/__tests__/issue-agent-mutation-ownership-routes.test.ts > \
agent issue mutation checkout ownership > uses the company-scope fast path on the issue list route
AssertionError: {"error":"Internal server error"}: expected 500 to be 200 // Object.is equality
##[error]Process completed with exit code 1."""
ROUTE_TEST = "src/__tests__/issue-agent-mutation-ownership-routes.test.ts"
RERAN = {**DESCRIBED, "reruns": {HEAD: [4]}}


def _traced(**files: str) -> dict:
    return {**RERAN, "ci_attribution": {"head_sha": HEAD, "files": files}}


def test_the_failing_test_files_are_read_off_the_runner_s_lines():
    assert followup.failing_tests({"job 40": LOG_16929, "job 41": "FAIL ui/src/a.test.tsx",
                                   "job 42": " FAIL  |cli| src/b.spec.ts > x"}) \
        == [ROUTE_TEST, "src/b.spec.ts", "ui/src/a.test.tsx"]


def test_ci_still_failing_runs_the_failing_tests_in_the_sandbox():
    step = _decide(_pr([_check("server", "failure", run_id=4)]), RERAN,
                   logs={"job 40": LOG_16929})
    assert (step.kind, step.tests) == ("wait", [ROUTE_TEST])


def test_a_failure_the_sandbox_traces_to_the_change_is_revised():
    step = _decide(_pr([_check("server", "failure", run_id=4)]),
                   _traced(**{ROUTE_TEST: followup.CAUSED}), logs={"job 40": LOG_16929})
    assert (step.kind, step.guidance, step.untraced) == ("revise", None, False)
    assert ROUTE_TEST in step.reason
    assert "pass without the change and fail with it" in (step.notes or "")
    assert "expected 500 to be 200" in (step.notes or "")


def test_a_failure_on_the_base_too_is_handed_back_with_the_evidence():
    step = _decide(_pr([_check("server", "failure", run_id=4)]),
                   _traced(**{ROUTE_TEST: followup.BASE_FAILS}), logs={"job 40": LOG_16929})
    assert step.kind == "hand-back" and f"{ROUTE_TEST} fails on the base too" in step.reason


def test_an_untraced_failure_gets_one_revision_before_it_is_handed_back():
    logs = {"job 40": "src/x.ts(3,1): error TS2304: Cannot find name 'y'."}
    pr = _pr([_check("typecheck", "failure", run_id=4)])
    step = _decide(pr, RERAN, logs=logs)
    assert (step.kind, step.untraced) == ("revise", True)
    assert "could not trace the failure" in (step.notes or "")
    step = _decide(pr, {**RERAN, "ci_untraced_revised": True}, logs=logs)
    assert step.kind == "hand-back" and "traced no failure to the change" in step.reason
    unknown = _decide(pr, {**_traced(**{ROUTE_TEST: followup.UNKNOWN})},
                      logs={"job 40": LOG_16929})
    assert (unknown.kind, unknown.untraced) == ("revise", True)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A held base whose clone carries the route test, and the sandbox's exits
    queued per run: the change's run first, then the base's."""
    clone = tmp_path / "clone"
    (clone / "server" / "src" / "__tests__").mkdir(parents=True)
    (clone / "server" / ROUTE_TEST).write_text("test")
    base = followup.prove.PinnedBase(sha="c" * 40, tier=1, image="img", clone=clone)
    held: list[tuple[str, int]] = []
    monkeypatch.setattr(followup.prove, "held", lambda sha, tier: held.append((sha, tier)) or base)
    monkeypatch.setattr(followup.prove, "compose", lambda label, *parts: tmp_path / "p.patch")
    exits: list[int] = []
    cmds: list[str] = []

    def run_command(base_, patch, cmd, **kw):
        cmds.append(cmd)
        return {"exit": exits.pop(0)}

    def run_phase(phase, image, **kw):
        assert phase == "repro" and "patch" not in kw
        cmds.append(kw["test_cmd"])
        return exits.pop(0), ""

    monkeypatch.setattr(followup.prove, "run_command", run_command)
    monkeypatch.setattr(followup.verify_driver, "run_phase", run_phase)
    return exits, cmds, held


RECORD = {"issue": 16267, "base_sha": "c" * 40, "base_tier": 1, "result": {"patch": PATCH}}


def test_a_test_that_fails_with_the_change_and_passes_on_the_base_is_the_change_s(sandbox):
    exits, cmds, held = sandbox
    exits[:] = [20, 0]
    assert followup.attribute(RECORD, [ROUTE_TEST]) == {ROUTE_TEST: followup.CAUSED}
    assert held == [("c" * 40, 1)]
    assert len(cmds) == 2 and all(f"server/{ROUTE_TEST}" in c for c in cmds)


def test_a_test_that_fails_on_the_base_too_or_passes_with_the_change_is_not(sandbox):
    exits, cmds, _ = sandbox
    exits[:] = [20, 20]
    assert followup.attribute(RECORD, [ROUTE_TEST]) == {ROUTE_TEST: followup.BASE_FAILS}
    exits[:] = [0]
    assert followup.attribute(RECORD, [ROUTE_TEST]) == {ROUTE_TEST: followup.PASSES}


def test_a_test_the_change_touches_is_the_change_s_without_a_run(sandbox):
    _, cmds, _ = sandbox
    assert followup.attribute(RECORD, ["ui/src/EmailMessageCard.tsx"]) \
        == {"ui/src/EmailMessageCard.tsx": followup.CAUSED}
    assert cmds == []


def test_a_test_with_no_single_path_or_no_held_base_is_unknown(sandbox, monkeypatch):
    _, cmds, _ = sandbox
    assert followup.attribute(RECORD, ["nowhere.test.ts", "../x.test.ts"]) \
        == {"nowhere.test.ts": followup.UNKNOWN, "../x.test.ts": followup.UNKNOWN}

    def no_base(sha, tier):
        raise followup.prove.NoBase("gone")

    monkeypatch.setattr(followup.prove, "held", no_base)
    assert followup.attribute(RECORD, [ROUTE_TEST]) == {ROUTE_TEST: followup.UNKNOWN}
    assert cmds == []


def test_a_poll_traces_the_failure_in_the_sandbox_and_revises(store, monkeypatch):
    pr = _pr([_check("server", "failure", run_id=4)])
    monkeypatch.setattr(followup, "read", lambda n: pr)
    monkeypatch.setattr(followup, "_job_log", lambda job: LOG_16929)
    traced = []
    monkeypatch.setattr(followup, "attribute", lambda record, names: traced.append(names) or {
        n: followup.CAUSED for n in names})
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "watching",
                                             "described_head": HEAD, "reruns": {HEAD: [4]}})
    followup.poll(store, mode="live")
    fu = store.load_issue(7).fix_followup
    assert traced == [[ROUTE_TEST]]
    assert fu["ci_attribution"] == {"head_sha": HEAD, "files": {ROUTE_TEST: followup.CAUSED}}
    assert fu["revisions"] == 1 and "ci_untraced_revised" not in fu
    assert ROUTE_TEST in store.load_issue(7).fix_request["notes"]


def test_a_reviewer_below_its_bar_sends_the_fix_back_with_its_findings():
    finding = {"path": "ui/a.tsx", "line": 32, "title": "Cached email details disappear",
               "body": "When connectors are off the provider supplies null."}
    step = _decide(_pr(views=[_greptile(reviewers.FAIL, findings=[finding],
                                        summary="Medium risk.")]))
    assert (step.kind, step.guidance) == ("revise", None)
    assert "ui/a.tsx:32" in (step.notes or "") and "Medium risk." in (step.notes or "")


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
    assert "guidance" not in req and "objected to the change" in req["notes"]
    assert store.load_issue(7).fix_followup["revisions"] == 1


def _run_follow_up(store: IssueStore, **words: str) -> str:
    fix_review.queue(store, 7, "send-back", by="followup", source="followup",
                     **(words or {"guidance": "g"}))
    _, outcome = fix_review_runner.run_request(store, 7, store.claim_fix_request(7, host="s"))
    return outcome


def test_a_follow_up_revision_carries_the_bots_findings_as_notes(store, monkeypatch):
    seen = {}

    def revise(store_, n, **kw):
        seen.update(kw)
        return {"ending": "no-fix", "detail": "nothing to change"}

    monkeypatch.setattr(fix_review_runner, "revise", revise)
    _run_follow_up(store, notes="CI fails in ui")
    assert (seen["comments"], seen["notes"]) == ("", "CI fails in ui")


def test_a_failed_follow_up_revision_leaves_the_proposal_as_it_was(store, monkeypatch, tmp_path):
    kept = (tmp_path / "issue-7" / "result.json").read_text()

    def failing_revise(store_, n, **kw):
        (tmp_path / "issue-7" / "result.json").write_text('{"ending": "no-fix"}')
        return {"ending": "no-fix", "detail": "nothing to change"}

    monkeypatch.setattr(fix_review_runner, "revise", failing_revise)
    assert "#9 is unchanged" in _run_follow_up(store)
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
    out = _run_follow_up(store)
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


def test_a_prospector_reviewer_is_read_from_the_store(monkeypatch):
    from pipeline import review_fetch
    stored = {"kind": "review", "reviewed_sha": HEAD, "checks": [], "extra": {},
              "findings": [{"severity": "blocking", "title": "dup", "resolved": False,
                            "outdated": False}]}
    monkeypatch.setattr(followup, "_stored_entry", lambda pr, rid: stored if rid == "design" else None)
    feed = review_fetch.PrFeed(pr=9, head_sha=HEAD, updated_at=None)
    assert followup._entry(reviewers.DESIGN, feed, HEAD, 9) == stored
    assert followup._entry(reviewers.GREPTILE, feed, HEAD, 9) is None


def test_review_evidence_keeps_suggested_code_and_drops_badges():
    finding = {"path": "a.tsx", "line": 1, "body": '<a href="#"><img alt="P1" src="x"></a> '
               "Fix it. ```suggestion <Ctx.Provider value={null}> ```"}
    step = _decide(_pr(views=[_greptile(reviewers.FAIL, findings=[finding])]))
    assert "<Ctx.Provider value={null}>" in (step.notes or "")
    assert "<img" not in (step.notes or "")


# --- maintainer feedback ------------------------------------------------------------

def _said(body: str = "Please keep the old flag working.", at: str = "2026-10-01T12:00:00Z"):
    from issue_triage import reply_router
    return reply_router.Reply(id=1, login="nicky", body=body, at=at)


def test_maintainer_feedback_comes_before_every_bot_signal():
    pr = _pr([_check("chat", "failure", run_id=4)], views=[_greptile(reviewers.FAIL)])
    step = _decide(pr, feedback="keep the flag")
    assert (step.kind, step.maintainer, step.guidance) == ("revise", True, "keep the flag")


def test_maintainer_feedback_releases_a_ready_hold():
    held = {**DESCRIBED, "state": "ready", "head_sha": HEAD}
    assert _decide(_pr(), held).kind == "wait"
    assert _decide(_pr(), held, feedback="rename it").kind == "revise"


def test_maintainer_revisions_have_their_own_budget():
    spent = {**DESCRIBED, "maintainer_revisions": followup.MAX_MAINTAINER_REVISIONS,
             "revisions": 0}
    step = _decide(_pr(), spent, feedback="again")
    assert step.kind == "hand-back" and step.maintainer
    assert _decide(_pr(views=[_greptile(reviewers.FAIL)]),
                   {**DESCRIBED, "revisions": followup.MAX_REVISIONS},
                   feedback="again").kind == "revise"


def test_the_feed_s_maintainer_words_are_the_feedback(monkeypatch):
    from pipeline import review_fetch
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "triagebot")
    feed = review_fetch.PrFeed(pr=9, head_sha=HEAD, updated_at=None, reviews=[
        {"id": 1, "login": "nicky", "association": "MEMBER", "state": "APPROVED", "body": "",
         "at": "2026-10-01T12:00:00Z"},
        {"id": 2, "login": "nicky", "association": "MEMBER", "state": "CHANGES_REQUESTED",
         "body": "", "at": "2026-10-01T12:01:00Z"},
        {"id": 3, "login": "passerby", "association": "NONE", "state": "COMMENTED",
         "body": "nit", "at": "2026-10-01T12:02:00Z"}],
        threads=[{"id": 4, "login": "nicky", "association": "MEMBER", "path": "a.ts", "line": 3,
                  "body": "rename this", "resolved": False, "at": "2026-10-01T12:03:00Z"},
                 {"id": 5, "login": "nicky", "association": "MEMBER", "path": "a.ts", "line": 9,
                  "body": "done", "resolved": True, "at": "2026-10-01T12:04:00Z"}],
        comments=[{"id": 6, "login": "triagebot", "association": "NONE", "body": "ready",
                   "at": "2026-10-01T12:05:00Z"},
                  {"id": 7, "login": "greptile-apps[bot]", "association": "NONE", "body": "5/5",
                   "at": "2026-10-01T12:06:00Z"}])
    said = followup._feedback(feed)
    assert [(r.body, r.where) for r in said] == [("(requested changes)", None),
                                                 ("rename this", "a.ts:3")]
    pr = _pr()
    pr = PrState(**{**pr.__dict__, "feedback": said})
    assert followup.fresh_feedback(pr, {"feedback_seen_at": "2026-10-01T12:01:00Z"}) == said[1:]


def test_a_live_poll_revises_on_a_maintainer_s_review(store, monkeypatch):
    from issue_triage import reply_router
    pr = PrState(**{**_pr().__dict__, "feedback": [_said()]})
    monkeypatch.setattr(followup, "read", lambda n: pr)
    monkeypatch.setattr(reply_router, "route", lambda context, rs: "retry")
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "ready", "head_sha": HEAD,
                                             "described_head": HEAD, "feedback_seen_at": ""})
    followup.poll(store, mode="live")
    issue = store.load_issue(7)
    req = issue.fix_request
    assert (req["action"], req["source"], req["requested_by"]) == ("send-back", "followup",
                                                                   "nicky")
    assert "> Please keep the old flag working." in req["guidance"]
    assert issue.fix_followup["maintainer_revisions"] == 1
    assert issue.fix_followup["feedback_seen_at"] == "2026-10-01T12:00:00Z"


def test_a_maintainer_s_approval_is_read_once_and_changes_nothing(store, monkeypatch):
    from issue_triage import reply_router
    pr = PrState(**{**_pr().__dict__, "feedback": [_said("LGTM, merging after the release")]})
    routed = []
    monkeypatch.setattr(followup, "read", lambda n: pr)
    monkeypatch.setattr(reply_router, "route", lambda context, rs: routed.append(rs) or "none")
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "ready", "head_sha": HEAD,
                                             "described_head": HEAD, "feedback_seen_at": ""})
    followup.poll(store, mode="live")
    followup.poll(store, mode="live")
    assert len(routed) == 1 and store.load_issue(7).fix_request is None


def test_feedback_waits_unread_while_the_ai_capacity_is_paused(store, monkeypatch):
    from issue_triage import reply_router
    pr = PrState(**{**_pr().__dict__, "feedback": [_said("LGTM, merging after the release")]})
    routed = []
    monkeypatch.setattr(followup, "read", lambda n: pr)
    monkeypatch.setattr(reply_router, "route", lambda context, rs: routed.append(rs) or "none")
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "ready", "head_sha": HEAD,
                                             "described_head": HEAD, "feedback_seen_at": ""})
    for _ in range(followup.MAX_ROUTE_MISSES + 1):
        followup.poll(store, mode="live", may_route=lambda: False)
    fu = store.load_issue(7).fix_followup
    assert routed == [] and not fu.get("feedback_misses") and fu["feedback_seen_at"] == ""
    followup.poll(store, mode="live", may_route=lambda: True)
    assert len(routed) == 1


def test_a_new_head_after_a_ready_reads_as_watching_until_judged_again(store, monkeypatch):
    moved = PrState(**{**_pr([_check(status="in_progress", conclusion=None)]).__dict__,
                       "head_sha": "b" * 40})
    monkeypatch.setattr(followup, "read", lambda pr: moved)
    store.edit_issue(7).record_fix_followup({
        "pr": 9, "state": "ready", "head_sha": HEAD, "described_head": "b" * 40,
        "judged": {"head_sha": HEAD, "reason": "green"}})
    followup.poll(store, mode="live")
    fu = store.load_issue(7).fix_followup
    assert fu["state"] == "watching" and fu["head_sha"] == "b" * 40


def test_a_ready_records_the_head_and_reason_it_was_judged_at(store, monkeypatch):
    monkeypatch.setattr(followup, "read", lambda pr: _pr())
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "watching",
                                             "described_head": HEAD})
    followup.poll(store, mode="live")
    fu = store.load_issue(7).fix_followup
    assert fu["state"] == "ready" and fu["judged"]["head_sha"] == HEAD



def test_a_maintainer_revision_that_did_not_land_hands_the_pull_request_back(store,
                                                                             monkeypatch):
    monkeypatch.setattr(followup, "read", lambda n: _pr())
    store.edit_issue(7).record_fix_followup({
        "pr": 9, "state": "watching", "head_sha": HEAD, "described_head": HEAD,
        "feedback_seen_at": "2026-10-01T12:00:00Z", "maintainer_pending": {"head_sha": HEAD}})
    store.edit_issue(7).record_fix_request({
        "action": "send-back", "status": "done", "source": "followup",
        "reason": "The revision ended no-fix: nothing to change; #9 is unchanged"})
    followup.poll(store, mode="live")
    fu = store.load_issue(7).fix_followup
    assert fu["state"] == "handed-back" and "did not land" in fu["judged"]["reason"]
    assert "maintainer_pending" not in fu


def test_a_follow_up_that_predates_feedback_routing_starts_from_now(store, monkeypatch):
    from issue_triage import reply_router
    pr = PrState(**{**_pr().__dict__, "feedback": [_said()]})
    monkeypatch.setattr(followup, "read", lambda n: pr)
    monkeypatch.setattr(reply_router, "route", lambda context, rs: pytest.fail("routed"))
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "watching", "head_sha": HEAD,
                                             "described_head": HEAD})
    followup.poll(store, mode="live")
    assert store.load_issue(7).fix_followup["feedback_seen_at"]


@pytest.mark.parametrize("ended,status,note", [
    ("closed", "pr-closed", "#9 was closed without merging; follow-up finished."),
    ("merged", "pr-merged", "#9 merged; follow-up finished."),
])
def test_a_poll_records_how_the_pull_request_ended_and_stops_reading_it(store, monkeypatch,
                                                                        ended, status, note):
    monkeypatch.setattr(followup, "read", lambda pr: _pr(state=ended))
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "watching",
                                             "described_head": HEAD})
    assert followup.poll(store, mode="live") == 1
    issue = store.load_issue(7)
    assert issue.fix_followup["state"] == "done" and issue.fix_followup["closed_as"] == ended
    assert issue.fix_thread[-1]["text"] == note
    assert fix_review.fix_status(issue)[0] == status
    monkeypatch.setattr(followup, "read", lambda pr: pytest.fail("read a finished proposal"))
    assert followup.poll(store, mode="live") == 0


def test_a_finished_follow_up_that_never_said_how_is_read_once_more(store, monkeypatch):
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "done", "step": "done",
                                             "reason": "#9 is closed"})
    notes = len(store.load_issue(7).fix_thread)
    reads = []
    monkeypatch.setattr(followup, "read", lambda pr: reads.append(pr) or _pr(state="closed"))
    followup.poll(store, mode="live")
    followup.poll(store, mode="live")
    issue = store.load_issue(7)
    assert reads == [9] and issue.fix_followup["closed_as"] == "closed"
    assert len(issue.fix_thread) == notes
    assert fix_review.fix_status(issue) == ("pr-closed", "#9 was closed without merging")


def test_a_new_proposal_is_followed_past_an_earlier_one_s_finished_record(store, monkeypatch):
    store.edit_issue(7).record_fix_followup({"pr": 5, "state": "done", "closed_as": "closed",
                                             "described_head": HEAD, "revisions": 2})
    monkeypatch.setattr(followup, "read", lambda pr: _pr())
    followup.poll(store, mode="dry-run")
    fu = store.load_issue(7).fix_followup
    assert fu["pr"] == 9 and fu["state"] == "watching" and fu["step"] == "describe"
    assert "closed_as" not in fu and "revisions" not in fu
    assert fix_review.fix_status(store.load_issue(7))[0] == "pr-open"


# --- rivals that open after the proposal ----------------------------------------------

def test_a_new_rival_is_judged_ahead_of_the_bot_signals_and_a_ready_hold():
    pr = _pr([_check("chat", "failure", run_id=4)])
    step = _decide(pr, rivals=[12])
    assert (step.kind, step.rivals) == ("second-opinion", [12])
    held = {**DESCRIBED, "state": "ready", "head_sha": HEAD}
    assert _decide(_pr(), held, rivals=[12]).kind == "second-opinion"
    assert _decide(_pr(), held).kind == "wait"
    assert _decide(pr, feedback="keep the flag", rivals=[12]).kind == "revise"


@pytest.fixture
def late(store, monkeypatch):
    """The open proposal #9 with one rival carrying tests (#12) and one
    without (#13) found by the search."""
    from issue_triage import second_opinion
    seen: list[set[int]] = []
    rival = second_opinion.Rival(pr=12, author="contrib", title="t", tests="T", fix="F",
                                 test_paths=["x.test.ts"])

    def rivals(n, *, exclude, registry=None):
        seen.append(set(exclude))
        fresh = [r for r in (rival,) if r.pr not in exclude]
        skipped = [{"pr": 13, "author": "x", "verdict": "skipped", "why": "no tests"}]
        return fresh, [e for e in skipped if e["pr"] not in exclude]

    monkeypatch.setattr(second_opinion, "rivals", rivals)
    monkeypatch.setattr(followup, "read", lambda n: _pr())
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "watching", "head_sha": HEAD,
                                             "described_head": HEAD})
    return seen


def test_a_live_poll_queues_one_judgment_per_new_rival(store, late):
    followup.poll(store, mode="live")
    issue = store.load_issue(7)
    req = issue.fix_request
    assert (req["action"], req["source"], req["rivals"]) == ("send-back", "followup", [12])
    assert issue.fix_followup["rivals_seen"] == [12, 13]
    store.edit_issue(7).record_fix_request({**req, "status": "done"})
    followup.poll(store, mode="live")
    assert late[-1] == {9, 12, 13}
    assert store.load_issue(7).fix_request["status"] == "done"
