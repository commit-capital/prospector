"""The fix agent driver: scoping, prompt assembly, and output validation.
run_agent is a subprocess boundary and is mocked; json_reply's real parsing runs
over the canned text."""
from __future__ import annotations

import json
import os

import pytest

from issue_triage import fix_issue, lane_check, reproduce_issue
from pipeline import headless_agent

CHANGES_REPLY = json.dumps({
    "summary": "Guard against empty input",
    "root_cause": "parse() indexes into an empty list",
    "changes": [{"path": "src/parse.ts", "rationale": "return early on empty input"}]})


def _run(monkeypatch, reply: str, worktree: str = "/wt", **over) -> dict:
    calls: dict = {}

    def fake(prompt, **kw):
        calls.update(kw, prompt=prompt)
        return reply

    monkeypatch.setattr(headless_agent, "run_agent", fake)
    kwargs = {"issue": 5, "title": "Crash on empty input",
              "body": "It throws when given nothing.",
              "test_paths": ["src/__tests__/repro.test.ts"],
              "preserve_paths": ["src/__tests__/keep.test.ts"],
              "red_tail": "TypeError: cannot read length of undefined",
              "withheld_globs": ("pipeline/**",), "env": {"K": "V"}}
    kwargs.update(over)
    return {"out": fix_issue.author(worktree, **kwargs), "calls": calls}


def test_fix_scopes_the_agent_to_its_clone(monkeypatch):
    env = {"PROSPECTOR_ISSUE_CHECK_ISSUE": "5"}
    c = _run(monkeypatch, CHANGES_REPLY, env=env)["calls"]
    assert c["read_root"] == ["/wt"]
    assert c["edit_root"] == "/wt"
    assert c["cwd"] == "/wt"
    assert c["allow"] == [f"Bash({lane_check.TOOL}:*)"]
    assert c["allow_gh"] is False
    assert "git_root" not in c
    assert c["env_extra"] == env
    assert set(c["env_allow"]) == {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
    assert c["timeout"] == 1800


def test_fix_hands_the_agent_resolved_paths(monkeypatch, tmp_path):
    real = tmp_path / "clone"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    calls: dict = {}

    def fake(prompt, **kw):
        calls.update(kw, prompt=prompt)
        return CHANGES_REPLY

    monkeypatch.setattr(headless_agent, "run_agent", fake)
    fix_issue.author(str(link), issue=5, title="t", body="b",
                     test_paths=["t/x.py"], preserve_paths=["t/k.py"], red_tail="boom",
                     withheld_globs=(), env={})
    resolved = os.path.realpath(str(link))
    assert calls["cwd"] == calls["edit_root"] == resolved
    assert calls["read_root"] == [resolved]
    assert resolved in calls["prompt"]


def test_a_well_formed_answer_parses(monkeypatch):
    out = _run(monkeypatch, CHANGES_REPLY)["out"]
    assert out["summary"] == "Guard against empty input"
    assert out["root_cause"] == "parse() indexes into an empty list"
    assert out["changes"] == [{"path": "src/parse.ts",
                               "rationale": "return early on empty input"}]


def test_give_up_parses(monkeypatch):
    out = _run(monkeypatch, json.dumps({"give_up": "the fix needs product judgment"}))["out"]
    assert out == {"give_up": "the fix needs product judgment"}


def test_an_answer_that_is_neither_changes_nor_give_up_is_an_error(monkeypatch):
    with pytest.raises(ValueError):
        _run(monkeypatch, json.dumps({"summary": "did stuff"}))


def test_a_change_entry_without_a_path_is_rejected(monkeypatch):
    with pytest.raises(ValueError):
        _run(monkeypatch, json.dumps({"summary": "s",
                                      "changes": [{"rationale": "no path"}]}))


def test_the_check_tool_and_test_paths_reach_the_prompt(monkeypatch):
    prompt = _run(monkeypatch, CHANGES_REPLY,
                  test_paths=["a/b_test.py", "c/d_test.py"])["calls"]["prompt"]
    assert f"{lane_check.TOOL} test" in prompt
    assert f"{lane_check.TOOL} typecheck" in prompt
    assert "a/b_test.py" in prompt
    assert "c/d_test.py" in prompt
    assert "src/__tests__/keep.test.ts" in prompt


def test_the_withheld_globs_reach_the_prompt(monkeypatch):
    prompt = _run(monkeypatch, CHANGES_REPLY,
                  withheld_globs=("pipeline/secret/**", "alert_triage/**"))["calls"]["prompt"]
    assert "pipeline/secret/**" in prompt
    assert "alert_triage/**" in prompt


def test_the_red_tail_reaches_the_prompt_and_is_capped(monkeypatch):
    prompt = _run(monkeypatch, CHANGES_REPLY, red_tail="Z" + "x" * 9000)["calls"]["prompt"]
    assert "x" * fix_issue.RED_TAIL_MAX in prompt
    # Capped to the tail: only the last RED_TAIL_MAX characters survive, so the
    # leading marker is gone.
    assert "Z" + "x" * 9000 not in prompt


def test_the_prompt_marks_the_report_and_output_untrusted(monkeypatch):
    prompt = _run(monkeypatch, CHANGES_REPLY)["calls"]["prompt"]
    assert "outsider" in prompt.lower()
    assert "data" in prompt.lower()


def test_the_report_reaches_the_prompt_without_re_substituting_a_token(monkeypatch):
    prompt = _run(monkeypatch, CHANGES_REPLY, title="Bug",
                  body="run __CHECK__ yourself")["calls"]["prompt"]
    # The report is embedded verbatim, so the __CHECK__ an outsider wrote into the
    # body survives the single-pass fill instead of turning into the tool path.
    assert reproduce_issue.report_block("Bug", "run __CHECK__ yourself") in prompt
    # while the prompt's own __CHECK__ token did become the tool path.
    assert f"{lane_check.TOOL} test" in prompt


def test_fix_propagates_a_run_agent_failure(monkeypatch):
    def crash(prompt, **kw):
        raise RuntimeError("claude did not exit within 1800s")

    monkeypatch.setattr(headless_agent, "run_agent", crash)
    with pytest.raises(RuntimeError):
        fix_issue.author("/wt", issue=5, title="t", body="b", test_paths=["t/x.py"],
                         preserve_paths=["t/k.py"], red_tail="boom", withheld_globs=(),
                         env={})
