"""Proposing a lane fix upstream: the rendered pull request, the gate on the
run, the fence on the push, and the push itself over local repositories."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from issue_triage import fix_lane, fix_pr_body, issue_gates, propose
from pipeline import settings

REPORT = "0123456789abcdef"
PATCH = """diff --git a/src/x.ts b/src/x.ts
--- a/src/x.ts
+++ b/src/x.ts
@@ -1 +1 @@
-export const x = 1;
+export const x = 2;
diff --git a/src/x.test.ts b/src/x.test.ts
new file mode 100644
--- /dev/null
+++ b/src/x.test.ts
@@ -0,0 +1 @@
+test('x', () => expect(x).toBe(2));
"""


def _result(**over) -> dict:
    return {"patch": PATCH, "summary": "Set x to two", "root_cause": "x was one",
            "changes": [{"path": "src/x.ts", "rationale": "set x"}],
            "proof": {"compile": {"exit": 0}, "suite": {"excluded": 2}},
            "tier": {"tier": 2}, **over}


def _record(**over) -> dict:
    return {"issue": 7, "ending": "fixed", "fault": False, "report_sha": REPORT,
            "base_sha": "a" * 40, "models": ["opus", "sonnet"], "result": _result(), **over}


# --- the rendering ---------------------------------------------------------------

def _rendered(result: dict | None = None) -> tuple[str, str, str]:
    res = result or _result()
    body = fix_pr_body.render(issue=7, result=res, tests=["src/x.test.ts"], base_sha="a" * 40,
                              report_sha=REPORT, models=["opus", "sonnet"], test_cmd=None)
    return (fix_pr_body.title(7, res["summary"]), body,
            fix_pr_body.commit_message(7, res["summary"]))


def test_a_rendered_proposal_has_no_problems():
    title, body, message = _rendered()
    assert title == "fix: Set x to two"
    assert fix_pr_body.problems(title, body, message, issue=7) == []
    assert body.count("Fixes #7") == 1 and "Fixes #7" in message


def test_each_required_section_carries_the_block_its_heading_names(monkeypatch):
    monkeypatch.setattr(fix_pr_body.describe_pr, "required_sections", lambda: (
        "Thinking Path", "What Changed", "Verification", "Risks", "Model Used", "Screenshots"))
    title, body, message = _rendered()
    assert fix_pr_body.problems(title, body, message, issue=7) == []
    sections = body.split("\n## ")
    heads = [part.split("\n", 1)[0] for part in sections[1:]]
    assert heads == ["Thinking Path", "What Changed", "Verification", "Risks", "Model Used",
                     "Screenshots", "Linked Issues"]
    by_head = {part.split("\n", 1)[0]: part for part in sections[1:]}
    assert "Root cause" in by_head["Thinking Path"]
    assert "`src/x.ts`: set x" in by_head["What Changed"]
    assert "- n/a" in by_head["Screenshots"]
    assert "Fixes #7" in by_head["Linked Issues"]


def test_a_tier_0_change_carries_a_warning():
    _, body, _ = _rendered(_result(tier={"tier": 0}))
    assert "[!WARNING]" in body and "highest risk (tier 0" in body
    _, body, _ = _rendered()
    assert "[!WARNING]" not in body


def test_changes_the_scope_reviewer_found_beyond_the_report_are_listed_under_risks():
    _, body, _ = _rendered(_result(reviews=[{"lens": "scope-safety", "verdict": "safe",
                                             "unasked": ["whitespace-only titles"]}]))
    risks = next(part for part in body.split("\n## ") if part.startswith("Risk"))
    assert "beyond the report: whitespace-only titles" in risks


def test_agent_text_is_held_to_inert_plain_text():
    cleaned = fix_pr_body._clean("see [docs](https://evil.com) and ![x](http://e) @bob\n"
                                 "<img src=x> `code` \u202eevil")
    assert cleaned == "see docs and x @\u200bbob 'code' evil"


def test_agent_text_cannot_close_another_issue():
    title, body, message = _rendered(_result(summary="fix things, closes #99"))
    assert "#99" in body
    assert any("rather than exactly #7" in p
               for p in fix_pr_body.problems(title, body, message, issue=7))


def test_a_foreign_link_or_live_mention_in_the_body_is_a_problem():
    title, body, message = _rendered()
    assert fix_pr_body.problems(title, body + "\nhttps://evil.example/x\n", message, issue=7)
    assert fix_pr_body.problems(title, body + "\ncc @someone\n", message, issue=7)
    repo_link = f"\nhttps://github.com/{settings.repo()}/issues/7\n"
    assert fix_pr_body.problems(title, body + repo_link, message, issue=7) == []


def test_a_body_missing_a_required_section_is_a_problem(monkeypatch):
    title, body, message = _rendered()
    monkeypatch.setattr(fix_pr_body.describe_pr, "required_sections",
                        lambda: ("What Changed", "Screenshots"))
    assert fix_pr_body.problems(title, body, message, issue=7) == [
        "the body lacks required sections: Screenshots"]


# --- the gate on the run -----------------------------------------------------------

def _live(**over) -> dict:
    return {"state": "open", "title": "x is wrong", "body": "x should be 2", **over}


def _gate(record: dict, live: dict | None) -> tuple[bool, str]:
    return issue_gates.propose_gate(record, live, report_sha=lambda t, b: REPORT)


def test_a_fixed_run_on_an_open_unedited_issue_may_be_proposed():
    assert _gate(_record(), _live()) == (True, "fixed, open, unedited, and clear")


@pytest.mark.parametrize("record,live,why", [
    (_record(ending="fix-unproven"), _live(), "not 'fixed'"),
    (_record(fault=True), _live(), "not 'fixed'"),
    (_record(result=_result(patch="")), _live(), "no patch"),
    (_record(), None, "cannot be read"),
    (_record(), _live(state="closed"), "closed"),
    (_record(report_sha="f" * 16), _live(), "edited"),
])
def test_a_run_that_may_not_be_proposed_says_why(record, live, why):
    ok, reason = _gate(record, live)
    assert not ok and why in reason


def test_the_gate_reads_the_report_with_the_lane_s_fingerprint():
    live = _live()
    record = _record(report_sha=fix_lane.report_sha(live["title"], live["body"]))
    assert issue_gates.propose_gate(record, live, report_sha=fix_lane.report_sha)[0]


def test_a_patch_that_scans_malicious_is_not_proposed(monkeypatch):
    monkeypatch.setattr(issue_gates.threats, "scan_diff", lambda d: {"verdict": "malicious"})
    assert _gate(_record(), _live()) == (False, "the patch scans malicious")


# --- the fence -----------------------------------------------------------------------

@pytest.fixture
def push_user(monkeypatch):
    monkeypatch.setenv("TRIAGE_PUSH_LOGIN", "pushbot")
    monkeypatch.setenv("TRIAGE_REPO", "up/proj")


def _fork(**over) -> dict:
    return {"full_name": "pushbot/proj", "fork": True, "parent": {"full_name": "up/proj"},
            "owner": {"login": "pushbot"}, "archived": False, "private": False, **over}


REF = f"prospector/issue-7-{REPORT[:8]}"


def test_the_fence_admits_the_push_user_s_fork_and_lane_branch(push_user):
    propose.assert_propose_target(_fork(), propose.fork_url(), REF, 7, REPORT[:8])


@pytest.mark.parametrize("fork,origin,ref,why", [
    (_fork(), "git@github.com:up/proj.git", REF, "not the push user's fork"),
    (None, None, REF, "cannot be read"),
    (_fork(fork=False), None, REF, "not a fork of up/proj"),
    (_fork(parent={"full_name": "other/proj"}), None, REF, "not a fork of up/proj"),
    (_fork(owner={"login": "someone"}), None, REF, "owned by 'someone'"),
    (_fork(archived=True), None, REF, "archived"),
    (_fork(), None, "prospector/issue-8-01234567", "not issue #7's lane branch"),
    (_fork(), None, "prospector/issue-7-01234567-x", "not issue #7's lane branch"),
    (_fork(), None, "main", "not issue #7's lane branch"),
])
def test_the_fence_refuses_any_other_destination(push_user, fork, origin, ref, why):
    with pytest.raises(propose.ProposeRefused, match=why):
        propose.assert_propose_target(fork, origin or propose.fork_url(), ref, 7, REPORT[:8])


def test_the_fence_refuses_without_a_push_login(monkeypatch):
    monkeypatch.delenv("TRIAGE_PUSH_LOGIN", raising=False)
    with pytest.raises(propose.ProposeRefused, match="no contributor-push login"):
        propose.assert_propose_target(_fork(), "x", REF, 7, REPORT[:8])


# --- the push, over local repositories ---------------------------------------------

def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repos(tmp_path, monkeypatch, push_user):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    work = tmp_path / "seed"
    work.mkdir()
    _git("init", "--quiet", "-b", settings.default_branch(), cwd=work)
    (work / "src").mkdir()
    (work / "src" / "x.ts").write_text("export const x = 1;\n")
    _git("add", ".", cwd=work)
    _git("commit", "--quiet", "-m", "base", cwd=work)
    base = _git("rev-parse", "HEAD", cwd=work).strip()
    upstream, fork = tmp_path / "upstream.git", tmp_path / "fork.git"
    _git("clone", "--quiet", "--bare", str(work), str(upstream))
    _git("clone", "--quiet", "--bare", str(work), str(fork))
    monkeypatch.setattr(propose, "upstream_url", lambda: str(upstream))
    monkeypatch.setattr(propose, "fork_url", lambda: str(fork))
    monkeypatch.setattr(propose, "fork_state", lambda: _fork())
    from prospector_app.backend import resubmit_identity
    monkeypatch.setattr(resubmit_identity, "push_env", lambda: {**os.environ, **env})
    return {"base": base, "fork": fork, "workdir": tmp_path / "propose"}


def _push(repos: dict, **over) -> propose.Pushed:
    kw = {"issue": 7, "report_sha": REPORT, "base_sha": repos["base"], "patch": PATCH,
          "message": fix_pr_body.commit_message(7, "Set x to two"),
          "workdir": repos["workdir"], "dry_run": False, **over}
    return propose.push_fix(**kw)


def test_a_push_lands_one_commit_on_the_base_at_the_lane_branch(repos):
    pushed = _push(repos)
    assert pushed.pushed and pushed.ref == REF
    fork = str(repos["fork"])
    assert _git("--git-dir", fork, "rev-parse", f"refs/heads/{REF}").strip() == pushed.head_sha
    assert _git("--git-dir", fork, "rev-parse", f"{REF}^").strip() == repos["base"]
    assert "Fixes #7" in _git("--git-dir", fork, "log", "-1", "--format=%B", REF)
    assert not repos["workdir"].exists()


def test_a_dry_run_pushes_nothing(repos):
    pushed = _push(repos, dry_run=True)
    assert not pushed.pushed
    assert not _git("--git-dir", str(repos["fork"]), "branch", "--list", REF).strip()


def test_an_existing_lane_branch_is_never_overwritten(repos):
    _push(repos)
    with pytest.raises(propose.ProposeRefused, match="already exists"):
        _push(repos)


def test_a_base_the_default_branch_does_not_hold_is_refused(repos):
    with pytest.raises(propose.ProposeRefused, match="not a full commit sha|not on"):
        _push(repos, base_sha="b" * 40)


def test_a_patch_that_no_longer_applies_is_refused(repos):
    with pytest.raises(propose.ProposeRefused, match="no longer applies"):
        _push(repos, patch=PATCH.replace("-export const x = 1;", "-export const x = 5;"))


def test_the_fence_runs_before_the_push(repos, monkeypatch):
    monkeypatch.setattr(propose, "fork_state", lambda: _fork(owner={"login": "someone"}))
    with pytest.raises(propose.ProposeRefused, match="owned by"):
        _push(repos)
    assert not _git("--git-dir", str(repos["fork"]), "branch", "--list", REF).strip()
