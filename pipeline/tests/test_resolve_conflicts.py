"""The conflict-resolution agent driver: prompt assembly, output validation,
and the fail-closed shape of what it returns. The agent itself is mocked —
run_agent is a subprocess boundary."""
from __future__ import annotations

import json

import pytest

from pipeline import headless_agent, resolve_conflicts


def _run(monkeypatch, reply: str) -> dict:
    calls: dict = {}

    def fake_run_agent(prompt, *, allow_gh, cwd, edit_root=None, timeout=0, on_event=None,
                       system_prompt=None, model=None,
                       read_root=None, env_allow=None, git_root=None):
        calls.update(read_root=read_root, env_allow=env_allow, git_root=git_root,
                     prompt=prompt, allow_gh=allow_gh, cwd=cwd, edit_root=edit_root)
        return reply

    monkeypatch.setattr(headless_agent, "run_agent", fake_run_agent)
    out = resolve_conflicts.resolve("/wt", ["a.ts", "b.ts"], pr=7, title="T",
                                    body="B", base_branch="master")
    return {"out": out, "calls": calls}


def test_resolve_returns_parsed_resolutions(monkeypatch):
    reply = json.dumps({"resolutions": [{"path": "a.ts", "rationale": "kept both"},
                                        {"path": "b.ts", "rationale": "took base"}]})
    r = _run(monkeypatch, reply)
    assert r["out"]["resolutions"][0]["path"] == "a.ts"
    assert r["calls"]["edit_root"] == "/wt"
    assert r["calls"]["cwd"] == "/wt"
    assert r["calls"]["allow_gh"] is False
    assert "a.ts" in r["calls"]["prompt"] and "master" in r["calls"]["prompt"]


def test_resolve_passes_give_up_through(monkeypatch):
    r = _run(monkeypatch, json.dumps({"give_up": "the sides contradict"}))
    assert r["out"] == {"give_up": "the sides contradict"}


def test_resolve_rejects_resolutions_for_unknown_paths(monkeypatch):
    reply = json.dumps({"resolutions": [{"path": "evil.ts", "rationale": "x"}]})
    with pytest.raises(ValueError):
        _run(monkeypatch, reply)


def test_resolve_rejects_missing_paths(monkeypatch):
    # every conflicted path must be accounted for
    reply = json.dumps({"resolutions": [{"path": "a.ts", "rationale": "x"}]})
    with pytest.raises(ValueError):
        _run(monkeypatch, reply)


def test_resolve_rejects_garbage(monkeypatch):
    with pytest.raises(ValueError):
        _run(monkeypatch, "I could not decide, sorry!")


def test_the_resolver_reads_and_runs_git_only_inside_its_worktree(monkeypatch):
    reply = json.dumps({"resolutions": [{"path": "a.ts", "rationale": "x"},
                                        {"path": "b.ts", "rationale": "y"}]})
    calls = _run(monkeypatch, reply)["calls"]
    assert calls["read_root"] == "/wt" and calls["git_root"] == "/wt"
    assert calls["cwd"] == "/wt" and list(calls["env_allow"]) == []
    assert f"{headless_agent.GIT_READ} diff" in calls["prompt"]
    assert "`git diff`" not in calls["prompt"]
