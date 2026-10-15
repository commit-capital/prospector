"""A chat is named from its first message: one tool-less Haiku run on Claude,
the question's own opening on any other provider or when the run fails."""
from __future__ import annotations

from fastapi.testclient import TestClient

from pipeline import headless_agent
from pipeline import settings
from prospector_app.backend import app as appmod
from prospector_app.backend import chat

QUESTION = ("oh man all 42 of these failed Design Review? can you look through those "
            "comments and tell me if you agree?")


def test_claude_summarizes_the_first_message_without_tools(monkeypatch):
    calls: list[dict] = []

    def fake_run(prompt: str, **kw) -> str:
        calls.append({"prompt": prompt, **kw})
        return '"Design Review Failures Audit."\n'

    monkeypatch.setattr(settings, "agent_provider", lambda: "claude")
    monkeypatch.setattr(headless_agent, "run_agent", fake_run)
    assert chat.chat_title(QUESTION) == "Design Review Failures Audit"
    [call] = calls
    assert QUESTION in call["prompt"]
    assert call["model"] == "haiku"
    assert call["allow_gh"] is False
    assert call["env_allow"] == ()
    assert call["read_root"] == call["cwd"]


def test_a_failed_run_falls_back_to_the_questions_opening(monkeypatch):
    def failing(prompt: str, **kw) -> str:
        raise headless_agent.AgentUnavailable("not logged in")

    monkeypatch.setattr(settings, "agent_provider", lambda: "claude")
    monkeypatch.setattr(headless_agent, "run_agent", failing)
    assert chat.chat_title(QUESTION) == "oh man all 42 of these failed Design Review?"


def test_another_provider_names_the_chat_without_an_agent(monkeypatch):
    monkeypatch.setattr(settings, "agent_provider", lambda: "codex")
    monkeypatch.setattr(headless_agent, "run_agent", lambda *a, **k: 1 / 0)
    assert chat.chat_title("Why is PR #12 blocked") == "Why is PR #12 blocked"


def test_fallback_clips_long_text_on_a_word_boundary():
    title = chat.fallback_title("word " * 40)
    assert len(title) <= chat.TITLE_MAX_CHARS
    assert title.endswith("word…")
    assert chat.fallback_title("   ") == "New chat"


def test_title_endpoint(temp_store, monkeypatch):
    monkeypatch.setattr(chat, "chat_title", lambda q: f"title of {q}")
    r = TestClient(appmod.app).post("/api/chat/title", json={"question": "hello"})
    assert r.status_code == 200
    assert r.json() == {"title": "title of hello"}
