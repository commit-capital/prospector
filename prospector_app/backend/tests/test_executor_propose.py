"""Opening an issue-fix pull request: the bot's allowlisted write and the
executor path that gates, pushes, opens and logs it."""
from __future__ import annotations

import json
import subprocess

import pytest

from issue_triage import fetch_issues, propose
from prospector_app.backend import activity, executor
from prospector_app.backend import safety_guard as sg

REPORT = "0123456789abcdef"
HEAD = f"pushbot:prospector/issue-7-{REPORT[:8]}"
PATCH = """diff --git a/src/x.ts b/src/x.ts
--- a/src/x.ts
+++ b/src/x.ts
@@ -1 +1 @@
-export const x = 1;
+export const x = 2;
"""


@pytest.fixture(autouse=True)
def push_user(monkeypatch):
    monkeypatch.setenv("TRIAGE_PUSH_LOGIN", "pushbot")
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "bot[bot]")


def _payload(**over) -> dict:
    return {"title": "fix: x", "body": "Fixes #7", "head": HEAD,
            "base": sg.settings.default_branch(),
            "maintainer_can_modify": True, **over}


def test_the_guard_admits_a_lane_branch_proposal():
    sg.assert_propose_write(_payload())


@pytest.mark.parametrize("payload,why", [
    (_payload(head="someone:prospector/issue-7-01234567"), "not the push user's lane branch"),
    (_payload(head="pushbot:main"), "not the push user's lane branch"),
    (_payload(base="release"), "targets"),
    (_payload(maintainer_can_modify=False), "allows maintainer edits"),
    (_payload(draft=False), "carries exactly"),
    ({k: v for k, v in _payload().items() if k != "body"}, "carries exactly"),
])
def test_the_guard_refuses_any_other_proposal(payload, why):
    with pytest.raises(sg.WriteAttemptBlocked, match=why):
        sg.assert_propose_write(payload)


def test_the_propose_write_refuses_without_a_token(monkeypatch):
    monkeypatch.setattr(sg.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(sg.WriteAttemptBlocked, match="without a"):
        sg.propose_bot_run(_payload(), "")


def test_the_propose_write_posts_the_payload_as_the_bot(monkeypatch):
    monkeypatch.setattr(sg, "assert_store_writes_safe", lambda: None)
    seen: dict = {}

    def fake_run(argv, *, input, env, **kw):
        seen.update(argv=argv, payload=json.loads(input), token=env["GH_TOKEN"])
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(sg.subprocess, "run", fake_run)
    sg.propose_bot_run(_payload(), "tok")
    assert seen["argv"][:5] == ["gh", "api", "--method", "POST",
                                f"repos/{sg.settings.repo()}/pulls"]
    assert seen["payload"] == _payload() and seen["token"] == "tok"


# --- the executor ---------------------------------------------------------------

@pytest.fixture
def lane(monkeypatch):
    state: dict = {
        "record": {"issue": 7, "ending": "fixed", "fault": False, "report_sha": REPORT,
                   "base_sha": "a" * 40, "models": ["opus"],
                   "result": {"patch": PATCH, "summary": "Set x to two", "root_cause": "r",
                              "changes": [{"path": "src/x.ts", "rationale": "set x"}],
                              "proof": {}, "tier": {"tier": 2}}},
        "live": {"state": "open", "title": "t", "body": "b"},
        "existing": [], "pushes": [], "posts": [], "events": [],
    }
    monkeypatch.setattr(propose, "load_result", lambda n: state["record"])
    monkeypatch.setattr(fetch_issues, "fetch_issue", lambda n: state["live"])
    from issue_triage import fix_lane
    monkeypatch.setattr(fix_lane, "report_sha", lambda t, b: REPORT)
    monkeypatch.setattr("pipeline.gh.gh_list", lambda path: state["existing"])

    def fake_push(**kw):
        state["pushes"].append(kw)
        return propose.Pushed(ref=propose.branch_ref(7, REPORT), head_sha="c" * 40,
                              tree_sha="d" * 40, pushed=not kw["dry_run"])

    monkeypatch.setattr(propose, "push_fix", fake_push)

    def fake_post(payload, token):
        state["posts"].append(payload)
        return subprocess.CompletedProcess([], 0, json.dumps(
            {"number": 42, "html_url": "https://github.com/up/proj/pull/42"}), "")

    monkeypatch.setattr(sg, "propose_bot_run", fake_post)
    monkeypatch.setattr(activity, "record",
                        lambda kind, **f: state["events"].append((kind, f)) or f)
    return state


def test_a_live_proposal_pushes_opens_and_logs(lane):
    res = executor.propose_issue_fix(7, token="tok", dry_run=False)
    assert res["status"] == "executed" and res["pr"] == 42
    assert lane["pushes"][0]["dry_run"] is False
    assert lane["posts"][0]["head"] == HEAD
    assert "Fixes #7" in lane["posts"][0]["body"]
    [(kind, event)] = lane["events"]
    assert kind == "issue-propose" and event["identity"] == "bot[bot]"
    assert event["dry_run"] is False and event["pr"] == 42


def test_without_a_token_a_live_request_is_a_dry_run(lane):
    res = executor.propose_issue_fix(7, token=None, dry_run=False)
    assert res["status"] == "dry-run" and res["forced"] is True
    assert lane["pushes"][0]["dry_run"] is True and lane["posts"] == []


def test_a_run_the_gate_refuses_is_blocked_before_any_push(lane):
    lane["live"] = {**lane["live"], "state": "closed"}
    res = executor.propose_issue_fix(7, token="tok", dry_run=False)
    assert res["status"] == "blocked" and "closed" in res["detail"]
    assert lane["pushes"] == [] and lane["events"][0][0] == "issue-propose"


def test_a_lane_branch_with_a_pull_request_is_reported_not_reopened(lane):
    lane["existing"] = [{"number": 41, "html_url": "u"}]
    res = executor.propose_issue_fix(7, token="tok", dry_run=False)
    assert res["status"] == "exists" and "#41" in res["detail"]
    assert lane["pushes"] == [] and lane["posts"] == []


def test_a_refused_push_opens_nothing(lane, monkeypatch):
    def refuse(**kw):
        raise propose.ProposeRefused("the fork cannot be read")

    monkeypatch.setattr(propose, "push_fix", refuse)
    res = executor.propose_issue_fix(7, token="tok", dry_run=False)
    assert res["status"] == "blocked" and "fork cannot be read" in res["detail"]
    assert lane["posts"] == []


def test_a_failed_open_after_a_push_is_an_error_naming_the_branch(lane, monkeypatch):
    monkeypatch.setattr(sg, "propose_bot_run",
                        lambda p, t: subprocess.CompletedProcess([], 1, "", "Validation Failed"))
    res = executor.propose_issue_fix(7, token="tok", dry_run=False)
    assert res["status"] == "error" and "Validation Failed" in res["detail"]
    assert "prospector/issue-7-" in res["detail"]
