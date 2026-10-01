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
    cleaned = fix_pr_body.inert("see [docs](https://evil.com) and ![x](http://e) @bob\n"
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


@pytest.mark.parametrize("ref", [REF, f"{REF}-2", f"{REF}-9"])
def test_the_fence_admits_the_push_user_s_fork_and_lane_branch(push_user, ref):
    propose.assert_propose_target(_fork(), propose.fork_url(), ref, 7, REPORT[:8])


def test_each_proposal_of_a_report_has_its_own_numbered_branch():
    assert propose.branch_ref(7, REPORT) == REF
    assert propose.branch_ref(7, REPORT, 2) == f"{REF}-2"
    assert [propose.attempt_of(r, 7, REPORT) for r in (REF, f"{REF}-2", f"{REF}-9")] == [1, 2, 9]
    for other in (f"{REF}-1", f"{REF}-10", f"{REF}-x", "prospector/issue-7-ffffffff-2"):
        assert propose.attempt_of(other, 7, REPORT) is None
    with pytest.raises(ValueError):
        propose.branch_ref(7, REPORT, propose.MAX_ATTEMPTS + 1)


@pytest.mark.parametrize("fork,origin,ref,why", [
    (_fork(), "git@github.com:up/proj.git", REF, "not the push user's fork"),
    (None, None, REF, "cannot be read"),
    (_fork(fork=False), None, REF, "not a fork of up/proj"),
    (_fork(parent={"full_name": "other/proj"}), None, REF, "not a fork of up/proj"),
    (_fork(owner={"login": "someone"}), None, REF, "owned by 'someone'"),
    (_fork(archived=True), None, REF, "archived"),
    (_fork(), None, "prospector/issue-8-01234567", "not issue #7's lane branch"),
    (_fork(), None, "prospector/issue-7-01234567-x", "not issue #7's lane branch"),
    (_fork(), None, "prospector/issue-7-01234567-1", "not issue #7's lane branch"),
    (_fork(), None, "prospector/issue-7-01234567-10", "not issue #7's lane branch"),
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


def test_a_lane_branch_already_holding_the_same_change_is_reused(repos):
    first = _push(repos)
    again = _push(repos)
    assert again.reused and not again.pushed and again.head_sha == first.head_sha


def test_a_lane_branch_holding_other_content_is_never_overwritten(repos):
    _push(repos)
    other = PATCH.replace("+export const x = 2;", "+export const x = 3;")
    with pytest.raises(propose.ProposeRefused, match="other content"):
        _push(repos, patch=other)


OTHER = PATCH.replace("+export const x = 2;", "+export const x = 4;")


def test_a_later_proposal_lands_on_its_own_branch_and_leaves_the_earlier_one(repos):
    first = _push(repos)
    second = _push(repos, patch=OTHER, attempt=2)
    fork = str(repos["fork"])
    assert second.pushed and second.ref == f"{REF}-2"
    assert _git("--git-dir", fork, "rev-parse", f"refs/heads/{REF}").strip() == first.head_sha
    assert _git("--git-dir", fork, "rev-parse", f"{REF}-2^").strip() == repos["base"]
    assert "x = 4" in _git("--git-dir", fork, "show", f"{REF}-2:src/x.ts")


def test_the_code_an_earlier_proposal_carried_is_not_proposed_again(repos):
    _push(repos)
    with pytest.raises(propose.ProposeRefused, match=f"{REF} already holds this same code"):
        _push(repos, attempt=2)
    assert not _git("--git-dir", str(repos["fork"]), "branch", "--list", f"{REF}-2").strip()


def test_an_earlier_branch_no_longer_on_the_fork_does_not_block_a_proposal(repos):
    assert _push(repos, attempt=3).ref == f"{REF}-3"


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


# --- a revision onto an open proposal ------------------------------------------------

REVISED = PATCH.replace("+export const x = 2;", "+export const x = 3;")


def _head(repos: dict) -> str:
    return _git("--git-dir", str(repos["fork"]), "rev-parse", f"refs/heads/{REF}").strip()


def _revise(repos: dict, **over) -> propose.Pushed:
    kw = {"issue": 7, "report_sha": REPORT, "base_sha": repos["base"], "patch": REVISED,
          "message": fix_pr_body.commit_message(7, "Set x to three"),
          "expected_head": _head(repos), "workdir": repos["workdir"], "dry_run": False, **over}
    return propose.push_revision(**kw)


def _advance_upstream(repos: dict, tmp_path: Path) -> str:
    work = tmp_path / "advance"
    upstream = propose.upstream_url()
    _git("clone", "--quiet", upstream, str(work))
    (work / "README.md").write_text("later\n")
    _git("add", ".", cwd=work)
    _git("commit", "--quiet", "-m", "later", cwd=work)
    _git("push", "--quiet", "origin", f"HEAD:{settings.default_branch()}", cwd=work)
    return _git("rev-parse", "HEAD", cwd=work).strip()


def test_a_revision_lands_one_commit_on_the_open_branch(repos):
    first = _push(repos)
    pushed = _revise(repos)
    fork = str(repos["fork"])
    assert pushed.pushed and _head(repos) == pushed.head_sha
    assert _git("--git-dir", fork, "rev-parse", f"{REF}^").strip() == first.head_sha
    assert "x = 3" in _git("--git-dir", fork, "show", f"{REF}:src/x.ts")


def test_a_revision_on_a_newer_base_merges_that_base_in(repos, tmp_path):
    first = _push(repos)
    newer = _advance_upstream(repos, tmp_path)
    pushed = _revise(repos, base_sha=newer)
    parents = _git("--git-dir", str(repos["fork"]), "rev-list", "--parents", "-n", "1",
                   pushed.head_sha).split()[1:]
    assert parents == [first.head_sha, newer]
    assert "later" in _git("--git-dir", str(repos["fork"]), "show", f"{REF}:README.md")


def test_a_branch_someone_else_moved_is_never_overwritten(repos):
    first = _push(repos)
    with pytest.raises(propose.ProposeRefused, match="someone else pushed"):
        _revise(repos, expected_head="f" * 40)
    assert _head(repos) == first.head_sha


def test_a_revision_goes_onto_the_numbered_branch_its_pull_request_is_open_from(repos):
    first = _push(repos)
    second = _push(repos, patch=OTHER, attempt=2)
    pushed = _revise(repos, attempt=2, expected_head=second.head_sha)
    fork = str(repos["fork"])
    assert pushed.ref == f"{REF}-2"
    assert _git("--git-dir", fork, "rev-parse", f"{REF}-2^").strip() == second.head_sha
    assert _head(repos) == first.head_sha


def test_an_unchanged_revision_pushes_nothing(repos):
    first = _push(repos)
    again = _revise(repos, patch=PATCH)
    assert again.reused and not again.pushed and _head(repos) == first.head_sha


def test_a_dry_run_revision_pushes_nothing(repos):
    first = _push(repos)
    assert not _revise(repos, dry_run=True).pushed
    assert _head(repos) == first.head_sha


# --- the checklist and the related-PR search ------------------------------------------

TEMPLATE = """## Checklist

- [ ] I have included a thinking path that traces from project context to this change
- [ ] I have specified the model used (with version and capability details)
- [ ] I have searched GitHub for duplicate or related PRs and linked them above
- [ ] My branch name describes the change and contains no internal ticket id
- [ ] I have added or updated tests where applicable
- [ ] All CI gates are green
"""
# The repository's own gate for the search affirmation, ported from its script.
DEDUP_RE = __import__("re").compile(
    r"^\s*[-*]\s*\[\s*([ xX])\s*\][^\n]*search(?:ed)?[^\n]*(?:similar|duplicate|prior)"
    r"[^\n]*\bprs?\b", __import__("re").IGNORECASE | __import__("re").MULTILINE)


def _body(**over) -> str:
    kw = {"issue": 7, "result": _result(), "tests": ["src/x.test.ts"], "base_sha": "a" * 40,
          "report_sha": REPORT, "models": ["opus"], "test_cmd": None, **over}
    return fix_pr_body.render(**kw)


def test_the_checklist_ticks_only_what_the_pipeline_did():
    body = _body(related=[], template=TEMPLATE)
    ticked = {line[6:40] for line in body.splitlines() if line.startswith("- [x]")}
    open_ = {line[6:40] for line in body.splitlines() if line.startswith("- [ ]")}
    assert any(t.startswith("I have searched") for t in ticked)
    assert any(t.startswith("My branch name") for t in open_)
    assert any(t.startswith("All CI gates") for t in open_)
    m = DEDUP_RE.search(body)
    assert m and m.group(1) == "x"


def test_without_a_search_the_search_box_stays_open():
    m = DEDUP_RE.search(_body(related=None, template=TEMPLATE))
    assert m and m.group(1) == " "


def test_the_search_s_findings_are_listed_under_the_linked_issue():
    body = _body(related=[{"number": 5, "title": "fix x", "state": "open", "author": "a"}],
                 template=TEMPLATE)
    assert "- #5 (open): fix x" in body
    assert "found no other pull request" in _body(related=[], template=TEMPLATE)
    title = fix_pr_body.title(7, "Set x to two")
    assert fix_pr_body.problems(title, body, fix_pr_body.commit_message(7, "x"), issue=7) == []
