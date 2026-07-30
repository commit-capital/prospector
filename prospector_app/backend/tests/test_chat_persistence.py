"""Where a thread persists: the transcript is stored in the shared SQL
chat_messages table (same DB as the store and activity), while the claude resume
session_id — a local-machine handle — stays in gitignored cache/."""
from __future__ import annotations

import asyncio
import json

from prospector_app.backend import chat


def test_save_splits_transcript_from_session_meta(temp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")

    chat._save("pr-7", "user", "is this safe to merge?", "sess-abc")
    chat._save("pr-7", "assistant", "yes — greptile 5/5, CI green", "sess-abc")

    # Transcript is in the DB, Q&A only, in insertion order, scoped to the operator.
    thread = chat.load_thread("pr-7")
    assert [m["role"] for m in thread] == ["user", "assistant"]
    assert thread[0]["text"] == "is this safe to merge?"

    # Session id is local-only, in cache/ — never in the shared DB.
    meta = tmp_path / "cache" / "chat" / "tester" / "pr-7.meta.json"
    assert json.loads(meta.read_text())["session_id"] == "sess-abc"


def test_orphaned_partial_recovered_on_load(temp_store, tmp_path, monkeypatch):
    """#191: a hot-reload kills the stream before the reply is saved; the partial
    sidecar is folded into the thread on the next load, so nothing is lost."""
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")

    chat._save("pr-9", "user", "explain the gate", None)
    chat._write_partial("pr-9", "The gate checks Greptile 5/5,")  # mid-answer when the reload hit
    assert "pr-9" not in chat._RUNNING                            # after a reload, nothing runs

    thread = chat.load_thread("pr-9")
    assert [m["role"] for m in thread] == ["user", "assistant"]
    assert thread[1]["text"] == "The gate checks Greptile 5/5,"
    # finalized away — the sidecar is gone and a second load doesn't duplicate it
    assert not chat._partial_path("pr-9").exists()
    assert [m["role"] for m in chat.load_thread("pr-9")] == ["user", "assistant"]


def test_live_partial_shown_but_not_finalized(temp_store, tmp_path, monkeypatch):
    """While a stream is live, the partial shows as a trailing bubble but stays a
    sidecar — the running stream commits the final reply itself."""
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")

    chat._save("pr-9", "user", "explain the gate", None)
    chat._write_partial("pr-9", "Looking…")
    chat._RUNNING["pr-9"] = object()  # a stream is live for this ctx
    try:
        thread = chat.load_thread("pr-9")
        assert [m["text"] for m in thread] == ["explain the gate", "Looking…"]
        assert chat._partial_path("pr-9").exists()  # NOT finalized — the live stream owns it
    finally:
        chat._RUNNING.pop("pr-9", None)


def test_each_operator_sees_only_their_own_thread(temp_store, tmp_path, monkeypatch):
    """Per-operator scoping: two operators writing to the same ctx_id each see
    only their own messages when they call load_thread."""
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    who = {"slug": "alice"}
    monkeypatch.setattr(chat, "_op_slug", lambda: who["slug"])

    chat._save("general", "user", "alice asks", None)
    who["slug"] = "bob"
    chat._save("general", "user", "bob asks", None)

    # bob's live view shows only bob's thread
    assert [m["text"] for m in chat.load_thread("general")] == ["bob asks"]
    # alice's live view shows only alice's thread
    who["slug"] = "alice"
    assert [m["text"] for m in chat.load_thread("general")] == ["alice asks"]


def test_thread_key_prefers_explicit_chat_id():
    """An operator-named session (#343) always wins over the subject-derived id,
    so its thread stays independent of whatever pr/cluster happens to be passed
    alongside it."""
    assert chat._thread_key("sess-abc", pr=42, cluster=None) == "sess-abc"
    assert chat._thread_key("sess-abc", pr=None, cluster=7) == "sess-abc"
    assert chat._thread_key("sess-abc", pr=None, cluster=None) == "sess-abc"


def test_thread_key_falls_back_to_subject_when_no_chat_id():
    """No chat_id (the pre-#343 caller shape) reproduces the exact legacy
    one-thread-per-subject key, unchanged."""
    assert chat._thread_key(None, pr=42, cluster=None) == chat._ctx_id(42, None) == "pr-42"
    assert chat._thread_key(None, pr=None, cluster=7) == chat._ctx_id(None, 7) == "cluster-7"
    assert chat._thread_key(None, pr=None, cluster=None) == chat._ctx_id(None, None) == "general"


def test_named_sessions_on_the_same_pr_keep_independent_threads(temp_store, tmp_path, monkeypatch):
    """Two named sessions both about PR #99 (e.g. one from weeks ago, one just
    started) don't bleed into each other — each chat_id is its own thread, even
    though both resolve the same pr/cluster subject context."""
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")

    old_key = chat._thread_key("sess-old", pr=99, cluster=None)
    new_key = chat._thread_key("sess-new", pr=99, cluster=None)
    assert old_key != new_key

    chat._save(old_key, "user", "weeks-old question", "sess-old-claude")
    chat._save(new_key, "user", "brand new question", "sess-new-claude")

    assert [m["text"] for m in chat.load_thread(old_key)] == ["weeks-old question"]
    assert [m["text"] for m in chat.load_thread(new_key)] == ["brand new question"]
    # Each session also keeps its own claude resume handle, so a fresh session
    # never silently resumes the other one's conversation.
    assert chat._session_id(old_key) == "sess-old-claude"
    assert chat._session_id(new_key) == "sess-new-claude"


def test_reground_marker_round_trip(temp_store, tmp_path, monkeypatch):
    """A stopped/silently-forked turn flags the thread; the next turn consumes the
    flag exactly once (read-and-clear)."""
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")

    assert chat._consume_reground("pr-6744") is False   # nothing pending by default
    chat._mark_reground("pr-6744")
    assert chat._reground_path("pr-6744").exists()
    assert chat._consume_reground("pr-6744") is True     # consumed…
    assert chat._consume_reground("pr-6744") is False    # …exactly once
    assert not chat._reground_path("pr-6744").exists()


def test_thread_digest_replays_recent_turns_within_budget(monkeypatch):
    """The replay labels each side, skips blank turns, and keeps the most-recent
    turns (in order) when the thread exceeds the budget."""
    monkeypatch.setattr(chat, "_REPLAY_BUDGET", 60)
    thread = [
        {"role": "user", "text": "OLDEST — dropped once the budget is exceeded"},
        {"role": "assistant", "text": ""},          # blank turns are skipped
        {"role": "user", "text": "recent question"},
        {"role": "assistant", "text": "recent answer"},
    ]
    out = chat._thread_digest(thread)
    assert "[operator] recent question" in out
    assert "[you] recent answer" in out
    assert "OLDEST" not in out                        # trimmed from the front
    assert out.index("recent question") < out.index("recent answer")  # order preserved


def test_stopped_turn_flags_reground(temp_store, tmp_path, monkeypatch):
    """After Stop, the next turn must re-ground: stop_chat pops _RUNNING, the
    stream sees the stop, and it flags the thread."""
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    monkeypatch.setattr(chat, "_bot_token", lambda: None)
    monkeypatch.setattr(chat, "system_prompt", lambda: "SYS")

    class FakeProc:
        pid = 4321
        def __init__(self):
            self.returncode = 0
            self.stdout = self._gen()
        async def _gen(self):
            # A long answer: one delta, then the operator hits Stop mid-stream.
            yield b'{"type":"stream_event","event":{"type":"content_block_delta",' \
                  b'"delta":{"type":"text_delta","text":"Let me check greptile"}},' \
                  b'"session_id":"sess-A"}\n'
            chat.stop_chat(pr=6744)  # operator presses Stop
        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        return FakeProc()

    monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        async for _ in chat.stream_chat("reanalyze 6744", pr=6744):
            pass
    asyncio.run(drive())

    assert chat._reground_path("pr-6744").exists()  # the stop armed a re-ground


def test_cancelled_stream_persists_partial_and_flags_reground(temp_store, tmp_path, monkeypatch):
    """#547: a dropped SSE connection — not an operator Stop — cancels the
    generator mid-stream, before claude's own "result" event ever arrives.
    sse_starlette does this by calling `aclose()` on the streaming generator
    when the client disconnects. The old code put its bookkeeping (save the
    partial reply, decide whether to re-ground) AFTER the try/finally, so the
    GeneratorExit skipped it — the next turn resumed a session with no context
    and no transcript replay, which is exactly "the agent forgot everything"."""
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    monkeypatch.setattr(chat, "_bot_token", lambda: None)
    monkeypatch.setattr(chat, "system_prompt", lambda: "SYS")
    monkeypatch.setattr(chat, "_build_context", lambda *a, **k: "SUBJECT-CONTEXT-555")

    class FakeProc:
        pid = 4321
        def __init__(self):
            self.returncode = None
            self.stdout = self._gen()
        async def _gen(self):
            yield b'{"type":"stream_event","event":{"type":"content_block_delta",' \
                  b'"delta":{"type":"text_delta","text":"Looking at the diff"}},' \
                  b'"session_id":"sess-CUT"}\n'
            # A real subprocess keeps streaming; the consumer disappears first.
            await asyncio.sleep(3600)
        async def wait(self):
            self.returncode = 0
            return 0
        def terminate(self):
            self.returncode = -15

    async def fake_exec(*cmd, **kw):
        return FakeProc()

    monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        gen = chat.stream_chat("explain this PR", pr=555)
        ev = await gen.__anext__()
        assert ev == {"event": "delta", "data": "Looking at the diff"}
        await gen.aclose()  # what sse_starlette does when the client disconnects
    asyncio.run(drive())

    thread = chat.load_thread("pr-555")
    assert thread[-1]["text"] == "Looking at the diff"    # partial reply persisted, not lost
    assert chat._reground_path("pr-555").exists()         # next turn must re-ground
    assert "pr-555" not in chat._RUNNING                   # cleaned up, not left dangling


def test_reground_turn_reinjects_context_and_replay_without_resume(
        temp_store, tmp_path, monkeypatch):
    """The turn after a lost session re-injects the subject context, replays the
    prior transcript, and starts a FRESH session (no `-r` of the dead one)."""
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    monkeypatch.setattr(chat, "_bot_token", lambda: None)
    monkeypatch.setattr(chat, "system_prompt", lambda: "SYS")
    monkeypatch.setattr(chat, "_build_context", lambda *a, **k: "SUBJECT-CONTEXT-6744")

    # A prior thread + a session handle, then a flag left by a stopped turn.
    chat._save("pr-6744", "user", "reanalyze 6744", "sess-OLD")
    chat._save("pr-6744", "assistant", "checking greptile…", "sess-OLD")
    chat._mark_reground("pr-6744")

    captured: dict[str, list[str]] = {}

    class FakeProc:
        pid = 4321
        def __init__(self):
            self.returncode = 0
            self.stdout = self._gen()
        async def _gen(self):
            yield b'{"type":"result","session_id":"sess-NEW"}\n'
        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        async for _ in chat.stream_chat("did greptile pass?", pr=6744):
            pass
    asyncio.run(drive())

    cmd = captured["cmd"]
    prompt = cmd[cmd.index("-p") + 1]
    assert "SUBJECT-CONTEXT-6744" in prompt            # subject re-injected
    assert "CONVERSATION SO FAR" in prompt              # transcript replayed…
    assert "reanalyze 6744" in prompt                   # …including a prior turn
    assert "-r" not in cmd                              # fresh session, dead one not resumed
    assert not chat._reground_path("pr-6744").exists()  # flag consumed
