"""/api/chat/history and /api/chat/stop accept an optional chat_id (#343) that
selects an operator-named session's own thread instead of the default
one-thread-per-subject — routed through chat._thread_key on both endpoints."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.backend import app as appmod
from app.backend import chat


def test_history_with_chat_id_reads_the_named_sessions_own_thread(
        temp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    chat._save("sess-alpha", "user", "about the alpha session", None)
    chat._save(chat._ctx_id(42, None), "user", "the default pr-42 thread", None)

    c = TestClient(appmod.app)
    r = c.get("/api/chat/history", params={"pr": 42, "chat_id": "sess-alpha"})
    assert r.status_code == 200
    body = r.json()
    assert body["ctx"] == "sess-alpha"
    assert [m["text"] for m in body["messages"]] == ["about the alpha session"]

    # Omitting chat_id falls back to the legacy pr-keyed thread, unaffected.
    r2 = c.get("/api/chat/history", params={"pr": 42})
    assert r2.json()["ctx"] == "pr-42"
    assert [m["text"] for m in r2.json()["messages"]] == ["the default pr-42 thread"]


class _FinishedProc:
    """A process that's already exited, so stop_chat's cleanup has nothing to
    terminate — only the _RUNNING bookkeeping is under test here."""
    returncode = 0


def test_stop_with_chat_id_targets_the_named_session_not_the_subject(
        temp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    # A stream is "running" for the named session but not for the bare subject.
    chat._RUNNING["sess-alpha"] = _FinishedProc()
    try:
        c = TestClient(appmod.app)
        assert c.post("/api/chat/stop", params={"pr": 42}).json()["stopped"] is False
        assert c.post("/api/chat/stop", params={"pr": 42, "chat_id": "sess-alpha"}).json()["stopped"] is True
        assert "sess-alpha" not in chat._RUNNING
    finally:
        chat._RUNNING.pop("sess-alpha", None)
