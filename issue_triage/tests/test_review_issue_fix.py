"""The refuting reviewer over an agent-authored issue fix. Nothing but an
explicit, well-formed `safe` gets through, and every returned dict names the
lens it judged under. The agent itself is mocked — run_agent is a subprocess
boundary."""
from __future__ import annotations

import json

import pytest

from issue_triage import issue_gates, review_issue_fix
from pipeline import headless_agent

PATCH = ("diff --git a/src/parse.ts b/src/parse.ts\n@@\n"
         "-  return xs[0]\n+  return xs[0] ?? null\n")


def _run(monkeypatch, reply, *, lens: str = "root-cause", **over) -> dict:
    calls: dict = {}

    def fake_run_agent(prompt, *, allow_gh, cwd, edit_root=None, timeout=0,
                       on_event=None, system_prompt=None, model=None,
                       read_root=None, env_allow=None, git_root=None):
        calls.update(read_root=read_root, env_allow=env_allow, git_root=git_root,
                     prompt=prompt, allow_gh=allow_gh, cwd=cwd, edit_root=edit_root,
                     timeout=timeout)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(headless_agent, "run_agent", fake_run_agent)
    kwargs = {"lens": lens, "title": "Crash on empty input",
              "body": "It throws when given nothing.",
              "root_cause": "parse() indexes into an empty list",
              "test_paths": ["src/__tests__/repro.test.ts"]}
    kwargs.update(over)
    return {"out": review_issue_fix.review("/wt", PATCH, **kwargs), "calls": calls}


def test_an_unknown_lens_is_rejected_before_any_agent_call(monkeypatch):
    called = {"n": 0}

    def fake(*a, **k):
        called["n"] += 1
        return ""

    monkeypatch.setattr(headless_agent, "run_agent", fake)
    with pytest.raises(ValueError):
        review_issue_fix.review("/wt", PATCH, lens="style", title="t", body="b",
                                root_cause="rc", test_paths=[])
    assert called["n"] == 0


def test_the_reviewer_is_read_only_under_the_bare_environment(monkeypatch):
    calls = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "r"}))["calls"]
    assert calls["read_root"] == "/wt" and calls["cwd"] == "/wt"
    assert calls["edit_root"] is None and calls["git_root"] is None
    assert list(calls["env_allow"]) == []
    assert calls["allow_gh"] is False
    assert calls["timeout"] == 900


def test_an_explicit_safe_verdict_passes_with_its_lens(monkeypatch):
    out = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "cures the cause",
                                        "concerns": []}), lens="root-cause")["out"]
    assert out["verdict"] == "safe"
    assert out["lens"] == "root-cause"
    assert out["reason"] == "cures the cause"


def test_an_unsafe_verdict_carries_its_lens_and_reason(monkeypatch):
    out = _run(monkeypatch, json.dumps({"verdict": "unsafe",
                                        "reason": "special-cases the input",
                                        "concerns": ["only handles []"]}),
               lens="scope-safety")["out"]
    assert out["verdict"] == "unsafe"
    assert out["lens"] == "scope-safety"
    assert out["concerns"] == ["only handles []"]


def test_a_missing_verdict_reads_as_unsafe_with_its_lens(monkeypatch):
    out = _run(monkeypatch, json.dumps({"reason": "no verdict"}))["out"]
    assert out["verdict"] == "unsafe"
    assert out["lens"] == "root-cause"
    assert "failed" not in out


def test_an_unrecognized_verdict_reads_as_unsafe(monkeypatch):
    out = _run(monkeypatch, json.dumps({"verdict": "mostly safe", "reason": "eh"}))["out"]
    assert out["verdict"] == "unsafe"
    assert "failed" not in out


def test_a_crashed_reviewer_reads_as_unsafe_and_failed(monkeypatch):
    out = _run(monkeypatch, RuntimeError("claude did not exit within 900s"))["out"]
    assert out["verdict"] == "unsafe"
    assert out["failed"] is True
    assert out["lens"] == "root-cause"
    assert "900s" in out["reason"]


def test_an_agent_outage_still_reads_as_unsafe_and_failed(monkeypatch):
    # A reviewer failure never reaches the push side, so an outage here is
    # swallowed as unsafe rather than propagated.
    out = _run(monkeypatch, headless_agent.AgentUnavailable("not logged in"))["out"]
    assert out["verdict"] == "unsafe"
    assert out["failed"] is True


def test_unparseable_output_reads_as_unsafe_and_failed(monkeypatch):
    out = _run(monkeypatch, "I think it's probably fine?")["out"]
    assert out["verdict"] == "unsafe"
    assert out["failed"] is True
    assert out["lens"] == "root-cause"


def test_the_per_lens_question_reaches_the_prompt(monkeypatch):
    root = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "r"}),
                lens="root-cause")["calls"]["prompt"]
    assert "special-case the reproduction's input" in root
    scope = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "r"}),
                 lens="scope-safety")["calls"]["prompt"]
    assert "break a caller it did not update" in scope


def test_the_report_and_root_cause_reach_the_prompt(monkeypatch):
    prompt = _run(monkeypatch,
                  json.dumps({"verdict": "safe", "reason": "r"}))["calls"]["prompt"]
    assert "parse() indexes into an empty list" in prompt
    assert "src/parse.ts" in prompt  # from the patch


def test_a_long_patch_is_clipped_with_its_tail_kept(monkeypatch):
    head = "H" * review_issue_fix.PATCH_HEAD_CHARS
    tail = "T" * review_issue_fix.PATCH_TAIL_CHARS
    middle = "M" * 50_000
    big = head + middle + tail
    calls: dict = {}

    def fake_run_agent(prompt, **kw):
        calls["prompt"] = prompt
        return json.dumps({"verdict": "safe", "reason": "r"})

    monkeypatch.setattr(headless_agent, "run_agent", fake_run_agent)
    review_issue_fix.review("/wt", big, lens="root-cause", title="t", body="b",
                            root_cause="rc", test_paths=[])
    assert "characters of this patch omitted" in calls["prompt"]
    assert tail in calls["prompt"]  # the tail is kept
    assert middle not in calls["prompt"]  # the middle is dropped


def test_every_returned_dict_carries_the_lens(monkeypatch):
    for reply in [json.dumps({"verdict": "safe", "reason": "r"}),
                  json.dumps({"verdict": "unsafe", "reason": "r"}),
                  "garbage",
                  RuntimeError("boom")]:
        out = _run(monkeypatch, reply, lens="scope-safety")["out"]
        assert out["lens"] == "scope-safety"


def test_every_review_lens_has_a_question():
    assert set(review_issue_fix._QUESTIONS) == set(issue_gates.REVIEW_LENSES)
