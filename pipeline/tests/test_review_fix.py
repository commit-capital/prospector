"""The refuting reviewer over an agent-authored patch. Everything here is about
one property: nothing but an explicit, well-formed `safe` gets through. The
agent itself is mocked — run_agent is a subprocess boundary."""
from __future__ import annotations

import json

import pytest

from pipeline import headless_agent, review_fix

PATCH = "diff --git a/src/retry.ts b/src/retry.ts\n@@\n-  while (true)\n+  for (…)\n"


def _run(monkeypatch, reply, **over) -> dict:
    calls: dict = {}

    def fake_run_agent(prompt, *, allow_gh, cwd, edit_root=None, timeout=0,
                       on_event=None, system_prompt=None, model=None):
        calls.update(prompt=prompt, allow_gh=allow_gh, cwd=cwd, edit_root=edit_root)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(headless_agent, "run_agent", fake_run_agent)
    kwargs = {"pr": 7, "goal": "Clear the outstanding review findings."}
    kwargs.update(over)
    return {"out": review_fix.review("/wt", PATCH, **kwargs), "calls": calls}


def test_an_explicit_safe_verdict_passes(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "bounded and local",
                                      "concerns": []}))
    assert r["out"]["verdict"] == "safe"
    assert r["out"]["reason"] == "bounded and local"


def test_an_unsafe_verdict_carries_its_reason(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "unsafe",
                                      "reason": "changes behavior for callers",
                                      "concerns": ["parse() now throws"]}))
    assert r["out"]["verdict"] == "unsafe"
    assert r["out"]["concerns"] == ["parse() now throws"]


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


def test_malformed_output_reads_as_unsafe(monkeypatch):
    # A reviewer that did not answer the question has not cleared anything.
    out = _run(monkeypatch, "I think it's probably fine?")["out"]
    assert out["verdict"] == "unsafe"


def test_an_unrecognized_verdict_reads_as_unsafe(monkeypatch):
    out = _run(monkeypatch, json.dumps({"verdict": "mostly safe", "reason": "eh"}))["out"]
    assert out["verdict"] == "unsafe"


def test_a_crashed_reviewer_reads_as_unsafe(monkeypatch):
    # The push side must never be reachable by killing the reviewer.
    out = _run(monkeypatch, RuntimeError("claude did not exit within 900s"))["out"]
    assert out["verdict"] == "unsafe"
    assert "900s" in out["reason"]


@pytest.mark.parametrize("reply", ["", "{}", json.dumps({"reason": "no verdict"})])
def test_every_empty_shaped_answer_reads_as_unsafe(monkeypatch, reply):
    assert _run(monkeypatch, reply)["out"]["verdict"] == "unsafe"


def test_the_review_summary_reaches_the_reviewer(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "ok"}),
             review_summary="The guard misses terminalResultSeen.")
    assert "misses terminalResultSeen" in r["calls"]["prompt"]
