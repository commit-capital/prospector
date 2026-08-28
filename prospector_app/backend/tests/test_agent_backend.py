from __future__ import annotations

import asyncio

from prospector_app.backend import agent_backend
from prospector_app.backend import chat


class FakeTurn:
    def __init__(self) -> None:
        self.session_id: str | None = "fake-session"
        self.completed = True
        self.stopped = False
        self.closed = False

    async def events(self):
        yield agent_backend.TextDelta("provider answer")

    async def close(self) -> None:
        self.closed = True


class FakeBackend:
    provider = "claude"

    def __init__(self) -> None:
        self.requests: list[agent_backend.AgentRequest] = []
        self.turn = FakeTurn()
        self.stopped: list[str] = []

    def readiness(self) -> dict[str, object]:
        return {"provider": self.provider, "ok": True, "adapter": "fake"}

    async def start(self, request: agent_backend.AgentRequest) -> FakeTurn:
        self.requests.append(request)
        return self.turn

    def stop(self, thread_key: str) -> bool:
        self.stopped.append(thread_key)
        return True

    def is_running(self, thread_key: str) -> bool:
        return False


def test_readiness_dispatches_to_the_configured_backend(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "claude")
    monkeypatch.setattr(chat, "_BACKENDS", {"claude": backend})

    assert chat.readiness() == {
        "provider": "claude", "ok": True, "adapter": "fake",
    }


def test_readiness_can_probe_a_provider_before_it_is_configured(monkeypatch):
    backend = FakeBackend()
    backend.provider = "codex"
    monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "claude")
    monkeypatch.setattr(chat, "_BACKENDS", {"codex": backend})

    assert chat.readiness("codex") == {
        "provider": "codex", "ok": True, "adapter": "fake",
    }


def test_chat_orchestration_consumes_normalized_backend_events(
        temp_store, tmp_path, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "claude")
    monkeypatch.setattr(chat, "_BACKENDS", {"claude": backend})
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    monkeypatch.setattr(chat, "_bot_token", lambda: None)
    monkeypatch.setattr(chat, "system_prompt", lambda: "AGENT-MANUAL")
    monkeypatch.setattr(chat, "_build_context", lambda *a, **k: "PR-CONTEXT")

    async def drive():
        return [event async for event in chat.stream_chat("is this safe?", pr=12)]

    events = asyncio.run(drive())

    assert events[0] == {"event": "delta", "data": "provider answer"}
    assert events[-1]["event"] == "done"
    assert backend.turn.closed is True
    assert backend.requests[0].thread_key == "pr-12"
    assert backend.requests[0].system_prompt == "AGENT-MANUAL"
    assert "PR-CONTEXT" in backend.requests[0].prompt
    assert chat.load_thread("pr-12")[-1]["text"] == "provider answer"


def test_stop_dispatches_to_the_backend(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(chat, "_BACKENDS", {"claude": backend})

    assert chat.stop_chat(pr=12) is True
    assert backend.stopped == ["pr-12"]
