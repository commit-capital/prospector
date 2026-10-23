"""The refuting reviewers over an agent-authored patch. Everything here is about
one property: nothing but an explicit, well-formed `safe` from every lens gets
through. The agent itself is mocked — run_agent is a subprocess boundary."""
from __future__ import annotations

import json

import pytest

from pipeline import authoring, headless_agent, review_fix

PATCH = "diff --git a/src/retry.ts b/src/retry.ts\n@@\n-  while (true)\n+  for (…)\n"


def _lens(prompt: str) -> str:
    return "design" if authoring.DESIGN_QUESTION in prompt else "scope-safety"


def _run(monkeypatch, reply, **over) -> dict:
    """Review PATCH with every lens answering `reply`, or each lens its own
    entry when `reply` is a dict keyed by lens."""
    calls: dict = {"prompts": {}}

    def fake_run_agent(prompt, *, allow_gh, cwd, edit_root=None, timeout=0,
                       on_event=None, system_prompt=None, model=None,
                       read_root=None, env_allow=None, git_root=None):
        calls.update(read_root=read_root, env_allow=env_allow, git_root=git_root,
                     prompt=prompt, allow_gh=allow_gh, cwd=cwd, edit_root=edit_root)
        calls["prompts"][_lens(prompt)] = prompt
        answer = reply[_lens(prompt)] if isinstance(reply, dict) else reply
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(headless_agent, "run_agent", fake_run_agent)
    kwargs = {"pr": 7, "goal": "Clear the outstanding review findings."}
    kwargs.update(over)
    return {"out": review_fix.review("/wt", PATCH, **kwargs), "calls": calls}


def test_an_explicit_safe_verdict_passes(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "bounded and local",
                                      "concerns": []}))
    assert r["out"]["verdict"] == "safe"
    assert "bounded and local" in r["out"]["reason"]


def test_an_unsafe_verdict_carries_its_reason(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "unsafe",
                                      "reason": "changes behavior for callers",
                                      "concerns": ["parse() now throws"]}))
    assert r["out"]["verdict"] == "unsafe"
    assert "scope-safety: parse() now throws" in r["out"]["concerns"]


def test_the_reviewer_cannot_write_or_reach_the_network(monkeypatch):
    # It judges a patch; it has no business editing the tree it is judging.
    r = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "ok"}))
    assert r["calls"]["edit_root"] is None
    assert r["calls"]["allow_gh"] is False
    assert r["calls"]["cwd"] == "/wt"


def test_the_patch_and_goal_reach_the_prompt(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "ok"}))
    assert "src/retry.ts" in r["calls"]["prompt"]
    assert "Clear the outstanding review findings." in r["calls"]["prompt"]


def test_malformed_output_reads_as_unsafe_and_as_no_verdict(monkeypatch):
    # A reviewer that did not answer the question has not cleared anything, and
    # has not judged anything either.
    out = _run(monkeypatch, "I think it's probably fine?")["out"]
    assert out["verdict"] == "unsafe"
    assert out["failed"] is True


def test_an_unrecognized_verdict_reads_as_unsafe(monkeypatch):
    # An answer that names a verdict, however hedged, is the reviewer's judgment.
    out = _run(monkeypatch, json.dumps({"verdict": "mostly safe", "reason": "eh"}))["out"]
    assert out["verdict"] == "unsafe"
    assert "failed" not in out


def test_a_crashed_reviewer_reads_as_unsafe_and_as_no_verdict(monkeypatch):
    # The push side must never be reachable by killing the reviewer.
    out = _run(monkeypatch, RuntimeError("claude did not exit within 900s"))["out"]
    assert out["verdict"] == "unsafe"
    assert "900s" in out["reason"]
    assert out["failed"] is True


@pytest.mark.parametrize("reply", ["", "{}", json.dumps({"reason": "no verdict"})])
def test_every_empty_shaped_answer_reads_as_unsafe(monkeypatch, reply):
    assert _run(monkeypatch, reply)["out"]["verdict"] == "unsafe"


def test_the_review_summary_reaches_the_reviewer(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "ok"}),
             review_summary="The guard misses terminalResultSeen.")
    assert "misses terminalResultSeen" in r["calls"]["prompt"]


def test_the_reviewer_reads_only_the_worktree_under_the_bare_environment(monkeypatch):
    calls = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "r"}))["calls"]
    assert calls["read_root"] == "/wt" and calls["cwd"] == "/wt"
    assert calls["edit_root"] is None and calls["git_root"] is None
    assert list(calls["env_allow"]) == []


SAFE = json.dumps({"verdict": "safe", "reason": "ok", "concerns": []})
DUPLICATED = json.dumps({
    "verdict": "unsafe", "reason": "re-derives a state the record already carries",
    "concerns": ["src/retry.ts:12 restates MAX_RETRIES from src/limits.ts:3"]})


def test_both_lenses_read_the_change(monkeypatch):
    prompts = _run(monkeypatch, SAFE)["calls"]["prompts"]
    assert set(prompts) == set(review_fix.LENSES) == {"scope-safety", "design"}
    assert all("src/retry.ts" in p for p in prompts.values())
    assert "behavior that changes beyond what the goal asked for" not in prompts["design"]


def test_a_safe_change_with_a_design_problem_is_rejected_and_says_why(monkeypatch):
    # The issue-fix lane's lesson: a change can be safe and still not one a
    # maintainer would merge under our name.
    out = _run(monkeypatch, {"scope-safety": SAFE, "design": DUPLICATED})["out"]
    assert out["verdict"] == "unsafe" and "failed" not in out
    assert out["reason"] == "design: re-derives a state the record already carries"
    assert out["concerns"] == ["design: src/retry.ts:12 restates MAX_RETRIES from src/limits.ts:3"]


def test_every_rejecting_lens_is_named(monkeypatch):
    unsafe = json.dumps({"verdict": "unsafe", "reason": "drops the guard",
                         "concerns": ["the cap is gone"]})
    out = _run(monkeypatch, {"scope-safety": unsafe, "design": DUPLICATED})["out"]
    assert "scope-safety: drops the guard" in out["reason"]
    assert "design: re-derives" in out["reason"]
    assert len(out["concerns"]) == 2


def test_a_crashed_design_reviewer_fails_closed(monkeypatch):
    out = _run(monkeypatch, {"scope-safety": SAFE,
                             "design": RuntimeError("claude did not exit within 900s")})["out"]
    assert out["verdict"] == "unsafe" and out["failed"] is True
    assert out["reason"].startswith("design: ") and "900s" in out["reason"]


def test_a_rejection_outranks_another_lens_failing_to_answer(monkeypatch):
    # A judgment on the change is something the author can answer; the failed
    # lens reads the revision afresh.
    out = _run(monkeypatch, {"scope-safety": "no json here", "design": DUPLICATED})["out"]
    assert out["verdict"] == "unsafe" and "failed" not in out
    assert out["reason"].startswith("design: ")


def test_the_design_question_is_the_issue_fix_lanes(monkeypatch):
    from issue_triage import review_issue_fix
    assert review_issue_fix._QUESTIONS["design"] == authoring.DESIGN_QUESTION
    assert authoring.DESIGN_QUESTION in review_fix._QUESTIONS["design"]
