"""The reproduction agent driver: scoping, prompt assembly, and output
validation. run_agent is a subprocess boundary and is mocked; json_reply's real
parsing runs over the canned text."""
from __future__ import annotations

import json
import os

import pytest

from issue_triage import lane_check, reproduce_issue
from pipeline import headless_agent

FILES_REPLY = json.dumps({
    "files": [{"path": "src/__tests__/repro.test.ts", "purpose": "reproduces the crash"}],
    "preserve": [{"path": "src/__tests__/keep.test.ts", "purpose": "a valid list still parses"}],
    "claimed_symptom": "throws on empty input",
    "expected_red_signature": "TypeError: cannot read length of undefined",
    "confidence": "high"})


def _run(monkeypatch, reply: str, worktree: str = "/wt", **over) -> dict:
    calls: dict = {}

    def fake(prompt, **kw):
        calls.update(kw, prompt=prompt)
        return reply

    monkeypatch.setattr(headless_agent, "run_agent", fake)
    kwargs = {"issue": 5, "title": "Crash on empty input",
              "body": "It throws when given nothing.", "env": {"K": "V"}}
    kwargs.update(over)
    return {"out": reproduce_issue.author(worktree, **kwargs), "calls": calls}


def test_reproduce_scopes_the_agent_to_its_clone(monkeypatch):
    env = {"PROSPECTOR_ISSUE_CHECK_ISSUE": "5"}
    c = _run(monkeypatch, FILES_REPLY, env=env)["calls"]
    assert c["read_root"] == ["/wt"]
    assert c["edit_root"] == "/wt"
    assert c["cwd"] == "/wt"
    assert c["allow"] == [f"Bash({lane_check.TOOL}:*)"]
    assert c["allow_gh"] is False
    assert "git_root" not in c
    assert c["env_extra"] == env
    assert set(c["env_allow"]) == {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
    assert c["timeout"] == 1800


def test_reproduce_hands_the_agent_resolved_paths(monkeypatch, tmp_path):
    real = tmp_path / "clone"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    calls: dict = {}

    def fake(prompt, **kw):
        calls.update(kw, prompt=prompt)
        return FILES_REPLY

    monkeypatch.setattr(headless_agent, "run_agent", fake)
    reproduce_issue.author(str(link), issue=5, title="t", body="b", env={})
    resolved = os.path.realpath(str(link))
    assert calls["cwd"] == calls["edit_root"] == resolved
    assert calls["read_root"] == [resolved]
    assert resolved in calls["prompt"]


def test_report_block_is_json_encoded_and_capped():
    block = reproduce_issue.report_block("Crash", "x" * 9000)
    obj = json.loads(block)
    assert obj["title"] == "Crash"
    assert obj["body"] == "x" * reproduce_issue.REPORT_MAX
    assert len(obj["body"]) == 8000


def test_the_report_reaches_the_prompt_without_re_substituting_a_token_in_it(monkeypatch):
    c = _run(monkeypatch, FILES_REPLY, title="Bug", body="run __CHECK__ yourself")["calls"]
    prompt = c["prompt"]
    # The report is embedded verbatim, so the __CHECK__ an outsider wrote into the
    # body survives the single-pass fill instead of turning into the tool path.
    assert reproduce_issue.report_block("Bug", "run __CHECK__ yourself") in prompt
    # while the prompt's own __CHECK__ token did become the tool path.
    assert f"{lane_check.TOOL} test" in prompt


def test_a_well_formed_answer_parses(monkeypatch):
    out = _run(monkeypatch, FILES_REPLY)["out"]
    assert out["files"] == [{"path": "src/__tests__/repro.test.ts",
                             "purpose": "reproduces the crash"}]
    assert out["preserve"] == [{"path": "src/__tests__/keep.test.ts",
                                "purpose": "a valid list still parses"}]
    assert out["claimed_symptom"] == "throws on empty input"
    assert out["expected_red_signature"] == "TypeError: cannot read length of undefined"
    assert out["confidence"] == "high"


def test_give_up_parses_with_its_kind(monkeypatch):
    out = _run(monkeypatch, json.dumps(
        {"give_up": "the defect needs a live browser", "kind": "needs-live-service"}))["out"]
    assert out == {"give_up": "the defect needs a live browser", "kind": "needs-live-service"}


def test_an_answer_that_is_neither_files_nor_give_up_is_an_error(monkeypatch):
    with pytest.raises(ValueError):
        _run(monkeypatch, json.dumps({"claimed_symptom": "throws"}))


def test_a_file_entry_without_a_path_is_rejected(monkeypatch):
    with pytest.raises(ValueError):
        _run(monkeypatch, json.dumps({"files": [{"purpose": "no path"}],
                                      "claimed_symptom": "throws"}))


def test_the_check_tool_and_test_conventions_reach_the_prompt(monkeypatch):
    prompt = _run(monkeypatch, FILES_REPLY)["calls"]["prompt"]
    assert f"{lane_check.TOOL} test <your test files>" in prompt
    assert "test conventions" in prompt


def test_the_prompt_marks_the_report_untrusted(monkeypatch):
    prompt = _run(monkeypatch, FILES_REPLY)["calls"]["prompt"]
    assert "outsider" in prompt.lower()
    assert "data" in prompt.lower()


def test_a_retry_note_becomes_a_previous_attempt_section(monkeypatch):
    prompt = _run(monkeypatch, FILES_REPLY,
                  retry_note="the file was not at a test path")["calls"]["prompt"]
    assert "## Your previous attempt" in prompt
    assert "the file was not at a test path" in prompt


def test_a_first_attempt_has_no_previous_attempt_section(monkeypatch):
    assert "## Your previous attempt" not in _run(monkeypatch, FILES_REPLY)["calls"]["prompt"]


def test_a_missing_preserve_list_reads_as_empty_for_the_host_to_refuse(monkeypatch):
    reply = json.loads(FILES_REPLY)
    del reply["preserve"]
    assert _run(monkeypatch, json.dumps(reply))["out"]["preserve"] == []


def test_a_preserve_entry_without_a_path_is_rejected(monkeypatch):
    reply = json.loads(FILES_REPLY)
    reply["preserve"] = [{"purpose": "no path"}]
    with pytest.raises(ValueError):
        _run(monkeypatch, json.dumps(reply))


def test_the_prompt_asks_for_preservation_tests(monkeypatch):
    prompt = _run(monkeypatch, FILES_REPLY)["calls"]["prompt"]
    assert "## Preservation tests" in prompt
    assert '"preserve"' in prompt
