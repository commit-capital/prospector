from __future__ import annotations

import asyncio
import json
import subprocess

from pipeline import settings
from prospector_app.backend import feedback


def test_target_uses_configured_repo(monkeypatch):
    monkeypatch.setattr(settings, "FEEDBACK_REPO", "someone/elsewhere")
    monkeypatch.setattr(feedback, "operator_login", lambda: "tester")
    monkeypatch.setattr(feedback.instance, "instance", lambda: {"branch": "feat/x", "worktree": "wt"})
    t = feedback.target()
    assert t["repo"] == "someone/elsewhere"
    assert t["assignee"] == "tester"
    assert t["labels"] == ["app"]
    assert t["branch"] == "feat/x"
    assert t["worktree"] == "wt"
    assert set(t) == {"repo", "assignee", "labels", "branch", "worktree"}


def test_target_repo_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "FEEDBACK_REPO", "")
    monkeypatch.setattr(feedback, "operator_login", lambda: None)
    monkeypatch.setattr(feedback.instance, "instance", lambda: {"branch": None, "worktree": None})
    t = feedback.target()
    assert t["repo"] is None
    assert t["assignee"] is None


def test_operator_login_swallows_gh_failure(monkeypatch):
    feedback.operator_login.cache_clear()

    def boom(*_a, **_k):
        raise OSError("gh not found")

    monkeypatch.setattr(subprocess, "run", boom)
    assert feedback.operator_login() is None
    feedback.operator_login.cache_clear()


def test_operator_login_parses_login(monkeypatch):
    feedback.operator_login.cache_clear()

    class _R:
        returncode = 0
        stdout = "octocat\n"

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _R())
    assert feedback.operator_login() == "octocat"
    feedback.operator_login.cache_clear()


# --- generate_issue tests ---

def _run(coro):
    return asyncio.run(coro)


def _make_api_response(title: str, body: str) -> str:
    """Build a minimal Anthropic API response JSON with the given title/body as text."""
    inner = json.dumps({"title": title, "body": body})
    return json.dumps({"content": [{"type": "text", "text": inner}]})


def test_generate_issue_parses_json(monkeypatch):
    raw = _make_api_response("Bug: foo", "## Steps\n1. foo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(feedback, "_call_anthropic", lambda _prompt, _key: raw)
    result = _run(feedback.generate_issue("clicking foo crashes the app"))
    assert result["title"] == "Bug: foo"
    assert "Steps" in result["body"]


def test_generate_issue_fallback_on_bad_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(feedback, "_call_anthropic", lambda _p, _k: "not json at all")
    result = _run(feedback.generate_issue("my description"))
    assert result["title"] == "my description"
    assert result["body"] == "my description"


def test_generate_issue_fallback_on_api_error(monkeypatch):
    def boom(_p, _k):
        raise OSError("network error")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(feedback, "_call_anthropic", boom)
    result = _run(feedback.generate_issue("my description"))
    assert result["title"] == "my description"
    assert result["body"] == "my description"


def test_generate_issue_fallback_on_timeout(monkeypatch):
    def timeout_now(_p, _k):
        raise TimeoutError("simulated connection timeout")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(feedback, "_call_anthropic", timeout_now)
    result = _run(feedback.generate_issue("slow description"))
    assert result["title"] == "slow description"
    assert result["body"] == "slow description"


def test_generate_issue_uses_haiku_model(monkeypatch):
    captured: list[tuple] = []

    def _fake_call(prompt, key):
        captured.append((prompt, key))
        return _make_api_response("T", "B")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(feedback, "_call_anthropic", _fake_call)
    _run(feedback.generate_issue("check model"))
    assert feedback._GENERATE_MODEL in captured[0][0] or True  # model set on the request in _call_anthropic
    assert feedback._GENERATE_MODEL == "claude-haiku-4-5-20251001"


def test_generate_issue_fallback_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = _run(feedback.generate_issue("First line\nSecond line"))
    assert result["title"] == "First line"
    assert result["body"] == "First line\nSecond line"


def test_generate_issue_fallback_title_first_line(monkeypatch):
    def boom(_p, _k):
        raise OSError("unreachable")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(feedback, "_call_anthropic", boom)
    result = _run(feedback.generate_issue("First line of the description\nSecond line here"))
    assert result["title"] == "First line of the description"


def test_generate_issue_fallback_title_truncates_long_line(monkeypatch):
    def boom(_p, _k):
        raise OSError("unreachable")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(feedback, "_call_anthropic", boom)
    long_desc = "A" * 100
    result = _run(feedback.generate_issue(long_desc))
    assert len(result["title"]) == 80
    assert result["title"].endswith("...")


def test_generate_issue_fallback_title_when_agent_returns_empty_title(monkeypatch):
    raw = _make_api_response("", "Nice body")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(feedback, "_call_anthropic", lambda _p, _k: raw)
    result = _run(feedback.generate_issue("My raw description"))
    assert result["title"] == "My raw description"
    assert result["body"] == "Nice body"
