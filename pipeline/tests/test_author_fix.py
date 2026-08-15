"""The fix-authoring agent driver: prompt assembly, output validation, and the
disclosure check that holds the agent to the edits it admits to. The agent
itself is mocked — run_agent is a subprocess boundary."""
from __future__ import annotations

import json

import pytest

from pipeline import author_fix, headless_agent

FINDINGS = [{"headline": "retry loop never exits", "class": "substantive",
             "why": "still outstanding in the current diff"},
            {"headline": "stray whitespace", "class": "nitpick", "why": "still there"}]


def _run(monkeypatch, reply: str, **over) -> dict:
    calls: dict = {}

    def fake_run_agent(prompt, *, allow_gh, cwd, edit_root=None, timeout=0,
                       on_event=None, system_prompt=None, model=None):
        calls.update(prompt=prompt, allow_gh=allow_gh, cwd=cwd, edit_root=edit_root,
                     timeout=timeout)
        return reply

    monkeypatch.setattr(headless_agent, "run_agent", fake_run_agent)
    kwargs = {"pr": 7, "title": "Fix the retry", "body": "It loops forever.",
              "goal": "Clear the outstanding review findings.",
              "findings": FINDINGS, "ci_failures": []}
    kwargs.update(over)
    return {"out": author_fix.author("/wt", **kwargs), "calls": calls}


def test_author_returns_the_parsed_changes(monkeypatch):
    reply = json.dumps({"summary": "bounded the retry loop",
                        "changes": [{"path": "src/retry.ts",
                                     "rationale": "added an attempt cap"}]})
    r = _run(monkeypatch, reply)
    assert r["out"]["changes"] == [{"path": "src/retry.ts",
                                    "rationale": "added an attempt cap"}]
    assert r["out"]["summary"] == "bounded the retry loop"


def test_the_agent_may_write_only_inside_the_worktree(monkeypatch):
    r = _run(monkeypatch, json.dumps({"summary": "s", "changes": []}))
    assert r["calls"]["edit_root"] == "/wt"
    assert r["calls"]["cwd"] == "/wt"


def test_the_goal_and_findings_reach_the_prompt(monkeypatch):
    r = _run(monkeypatch, json.dumps({"summary": "s", "changes": []}))
    prompt = r["calls"]["prompt"]
    assert "Clear the outstanding review findings." in prompt
    assert "retry loop never exits" in prompt


def test_the_prompt_marks_its_inputs_untrusted(monkeypatch):
    # Findings, PR text and CI output are contributor-controlled, and this agent
    # has write access to a tree that gets pushed.
    r = _run(monkeypatch, json.dumps({"summary": "s", "changes": []}))
    assert "untrusted" in r["calls"]["prompt"].lower()


def test_gh_is_withheld_when_there_is_no_ci_lane(monkeypatch):
    r = _run(monkeypatch, json.dumps({"summary": "s", "changes": []}))
    assert r["calls"]["allow_gh"] is False


def test_gh_is_granted_to_read_failing_checks(monkeypatch):
    # The store keeps `signals.ci` as a bare verdict, so failing job output can
    # only be read live.
    r = _run(monkeypatch, json.dumps({"summary": "s", "changes": []}),
             ci_failures=["build (ubuntu-latest)"])
    assert r["calls"]["allow_gh"] is True
    assert "build (ubuntu-latest)" in r["calls"]["prompt"]


def test_author_passes_give_up_through(monkeypatch):
    r = _run(monkeypatch, json.dumps({"give_up": "the finding needs a product call"}))
    assert r["out"] == {"give_up": "the finding needs a product call"}


def test_malformed_output_is_an_error_not_a_change(monkeypatch):
    with pytest.raises(ValueError):
        _run(monkeypatch, json.dumps({"summary": "did stuff"}))


def test_a_change_entry_without_a_path_is_rejected(monkeypatch):
    with pytest.raises(ValueError):
        _run(monkeypatch, json.dumps({"summary": "s",
                                      "changes": [{"rationale": "no path"}]}))


# --- the disclosure check ------------------------------------------------------

def test_disclosure_accepts_a_patch_matching_what_was_reported():
    author_fix.assert_disclosed([{"path": "a.ts", "rationale": "r"}], ["a.ts"])


def test_an_undisclosed_edit_is_refused():
    # The agent edited something it did not admit to; the patch is not trusted.
    with pytest.raises(ValueError, match="did not report"):
        author_fix.assert_disclosed([{"path": "a.ts", "rationale": "r"}],
                                    ["a.ts", "secrets.ts"])


def test_a_reported_path_the_patch_does_not_contain_is_refused():
    with pytest.raises(ValueError, match="reported"):
        author_fix.assert_disclosed([{"path": "a.ts", "rationale": "r"},
                                     {"path": "b.ts", "rationale": "r"}], ["a.ts"])
