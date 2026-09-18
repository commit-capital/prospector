"""The reproduction-judge driver: read-only scoping, prompt assembly, and the
soft-failure behavior that turns a crash or an unparseable answer into a machine
fault while letting an agent outage propagate. run_agent is mocked."""
from __future__ import annotations

import json
import os
from collections.abc import Callable

import pytest

from issue_triage import judge_repro, reproduce_issue
from pipeline import headless_agent

SURE = {"symptom_match": {"matches": True, "confidence": "high", "reasoning": "clear"},
        "defect": {"is_defect": True, "confidence": "medium", "reasoning": "real"}}
FILES = [{"path": "src/__tests__/repro.test.ts",
          "contents": "test('crashes', () => { expect(f()).toBe(1); });\n"}]


def _fenced(obj: dict) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


def _judge(monkeypatch, run: Callable[..., str], worktree: str = "/wt", **over) -> dict:
    calls: dict = {}

    def fake(prompt, **kw):
        calls.update(kw, prompt=prompt)
        return run(prompt, **kw)

    monkeypatch.setattr(headless_agent, "run_agent", fake)
    kwargs = {"title": "Bug", "body": "empty input crashes", "files": FILES,
              "claimed_symptom": "throws on empty input",
              "expected_red_signature": "TypeError", "red_tail": "AssertionError: x"}
    kwargs.update(over)
    return {"out": judge_repro.judge(worktree, **kwargs), "calls": calls}


def test_judge_reads_the_tree_and_writes_nothing(monkeypatch):
    c = _judge(monkeypatch, lambda p, **k: _fenced(SURE))["calls"]
    assert c["read_root"] == ["/wt"]
    assert c["cwd"] == "/wt"
    assert c["allow_gh"] is False
    assert c["env_allow"] == ()
    assert c["timeout"] == 900
    assert "edit_root" not in c
    assert "git_root" not in c
    assert "allow" not in c
    assert "env_extra" not in c


def test_judge_hands_the_agent_resolved_paths(monkeypatch, tmp_path):
    real = tmp_path / "clone"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    resolved = os.path.realpath(str(link))
    c = _judge(monkeypatch, lambda p, **k: _fenced(SURE), worktree=str(link))["calls"]
    assert c["cwd"] == resolved
    assert c["read_root"] == [resolved]


def test_a_well_formed_rating_is_returned_as_is(monkeypatch):
    assert _judge(monkeypatch, lambda p, **k: _fenced(SURE))["out"] == SURE


def test_a_crash_becomes_a_soft_failure(monkeypatch):
    def crash(prompt, **kw):
        raise RuntimeError("claude did not exit within 900s")

    out = _judge(monkeypatch, crash)["out"]
    assert out["failed"] is True
    assert "900s" in out["reason"]


def test_an_agent_outage_propagates(monkeypatch):
    def outage(prompt, **kw):
        raise headless_agent.AgentUnavailable("Not logged in")

    with pytest.raises(headless_agent.AgentUnavailable):
        _judge(monkeypatch, outage)


def test_a_declined_prompt_propagates(monkeypatch):
    def declined(prompt, **kw):
        raise headless_agent.AgentDeclined("safeguards flagged this message")

    with pytest.raises(headless_agent.AgentDeclined):
        _judge(monkeypatch, declined)


def test_unparseable_output_becomes_a_soft_failure(monkeypatch):
    out = _judge(monkeypatch, lambda p, **k: "I could not decide.")["out"]
    assert out["failed"] is True


def test_a_structurally_malformed_rating_becomes_a_soft_failure(monkeypatch):
    # The defect section is missing, so the rating is not the shape the gate reads.
    out = _judge(monkeypatch, lambda p, **k: _fenced(
        {"symptom_match": {"matches": True, "confidence": "high", "reasoning": "r"}}))["out"]
    assert out["failed"] is True


def test_a_non_boolean_verdict_flag_becomes_a_soft_failure(monkeypatch):
    bad = {"symptom_match": {"matches": "yes", "confidence": "high", "reasoning": "r"},
           "defect": {"is_defect": True, "confidence": "high", "reasoning": "r"}}
    assert _judge(monkeypatch, lambda p, **k: _fenced(bad))["out"]["failed"] is True


def test_the_red_tail_is_capped(monkeypatch):
    prompt = _judge(monkeypatch, lambda p, **k: _fenced(SURE),
                    red_tail="z" * 7000)["calls"]["prompt"]
    assert "z" * 6000 in prompt
    assert "z" * 6001 not in prompt


def test_the_files_claim_signature_and_report_reach_the_prompt(monkeypatch):
    prompt = _judge(monkeypatch, lambda p, **k: _fenced(SURE),
                    claimed_symptom="throws on empty", expected_red_signature="TypeError: empty",
                    title="Crash", body="empty input crashes")["calls"]["prompt"]
    assert "src/__tests__/repro.test.ts" in prompt
    assert "throws on empty" in prompt
    assert "TypeError: empty" in prompt
    assert reproduce_issue.report_block("Crash", "empty input crashes") in prompt


def test_the_prompt_marks_report_and_output_untrusted_and_reserves_the_decision(monkeypatch):
    prompt = _judge(monkeypatch, lambda p, **k: _fenced(SURE))["calls"]["prompt"]
    assert "data" in prompt.lower()
    assert "you do not decide" in prompt.lower()


def test_a_non_string_confidence_becomes_a_soft_failure(monkeypatch):
    bad = {"symptom_match": {"matches": True, "confidence": 9, "reasoning": "r"},
           "defect": {"is_defect": True, "confidence": "high", "reasoning": "r"}}
    assert _judge(monkeypatch, lambda p, **k: _fenced(bad))["out"]["failed"] is True


def test_a_non_string_reasoning_becomes_a_soft_failure(monkeypatch):
    bad = {"symptom_match": {"matches": True, "confidence": "high", "reasoning": 1},
           "defect": {"is_defect": True, "confidence": "high", "reasoning": "r"}}
    assert _judge(monkeypatch, lambda p, **k: _fenced(bad))["out"]["failed"] is True


def test_a_token_in_the_report_is_not_re_substituted_into_the_judge_prompt(monkeypatch):
    prompt = _judge(monkeypatch, lambda p, **k: _fenced(SURE),
                    title="Bug", body="run __RED_TAIL__ yourself")["calls"]["prompt"]
    assert reproduce_issue.report_block("Bug", "run __RED_TAIL__ yourself") in prompt
