"""The refuting reviewers over an agent's merge-conflict resolution. Same
property as review_fix: nothing but an explicit, well-formed `safe` gets
through. run_agent is a subprocess boundary, mocked here."""
from __future__ import annotations

import json

import pytest

from pipeline import headless_agent, review_resolve

PATCH = "diff --git a/src/app.py b/src/app.py\n@@\n-    return x\n+    return x or 'both'\n"
MERGE_DIFF = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> abc123\n"


def _run(monkeypatch, reply, **over) -> dict:
    calls: dict = {}

    def fake_run_agent(prompt, *, allow_gh, cwd, edit_root=None, timeout=0,
                       on_event=None, system_prompt=None, model=None):
        calls.update(prompt=prompt, allow_gh=allow_gh, cwd=cwd, edit_root=edit_root)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(headless_agent, "run_agent", fake_run_agent)
    kwargs = dict(pr=7, title="fix: handle None", merge_diff=MERGE_DIFF,
                  patch=PATCH,
                  resolutions=[{"path": "src/app.py", "rationale": "kept both"}],
                  history="src/app.py:\n  commits on this PR's side:\n    abc #101",
                  store_context="stops the retry loop", lens="behavior")
    kwargs.update(over)
    return {"out": review_resolve.review("/wt", **kwargs), "calls": calls}


def test_an_explicit_safe_verdict_passes(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "both kept",
                                      "concerns": []}))
    assert r["out"]["verdict"] == "safe"
    assert r["out"]["reason"] == "both kept"


def test_an_unsafe_verdict_carries_its_reason(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "unsafe",
                                      "reason": "drops the base side's guard",
                                      "concerns": ["null check gone"]}))
    assert r["out"]["verdict"] == "unsafe"
    assert r["out"]["concerns"] == ["null check gone"]


def test_a_malformed_answer_is_unsafe_and_failed(monkeypatch):
    r = _run(monkeypatch, "I think it looks fine!")
    assert r["out"]["verdict"] == "unsafe"
    assert r["out"].get("failed") is True


def test_a_crashed_reviewer_is_unsafe_and_failed(monkeypatch):
    r = _run(monkeypatch, RuntimeError("agent timed out"))
    assert r["out"]["verdict"] == "unsafe"
    assert r["out"].get("failed") is True


def test_a_judged_unsafe_is_not_marked_failed(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "unsafe", "reason": "regression"}))
    assert "failed" not in r["out"]


def test_the_reviewer_cannot_write_or_reach_the_network(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "ok"}))
    assert r["calls"]["edit_root"] is None
    assert r["calls"]["allow_gh"] is False
    assert r["calls"]["cwd"] == "/wt"


def test_the_prompt_carries_the_evidence(monkeypatch):
    r = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "ok"}))
    p = r["calls"]["prompt"]
    assert MERGE_DIFF.strip() in p
    assert PATCH.strip() in p
    assert "kept both" in p          # the resolver's rationale
    assert "#101" in p               # the per-side history
    assert "stops the retry loop" in p


def test_each_lens_gets_its_own_charge(monkeypatch):
    behavior = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "ok"}),
                    lens="behavior")["calls"]["prompt"]
    hist = _run(monkeypatch, json.dumps({"verdict": "safe", "reason": "ok"}),
                lens="history")["calls"]["prompt"]
    assert behavior != hist


def test_an_unknown_lens_is_rejected():
    with pytest.raises(ValueError):
        review_resolve.review("/wt", pr=7, title="t", merge_diff="", patch="d",
                              resolutions=[], history="", store_context="",
                              lens="vibes")
