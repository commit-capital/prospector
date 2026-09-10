"""Deterministic action receipts for embedded chat.

These tests exercise pure parsing/validation only. They never spawn Claude or
make a network call.
"""
import asyncio
import json

from prospector_app.backend import chat
from prospector_app.backend import claude_backend
from prospector_app.backend import issue_receipts
from pipeline import settings


def _receipt(number: int = 50) -> issue_receipts.IssueReceipt:
    return {
        "ok": True,
        "kind": "feedback-issue",
        "repo": settings.feedback_repo(),
        "number": number,
        "url": f"https://github.com/{settings.feedback_repo()}/issues/{number}",
    }


def test_file_issue_receipt_requires_command_and_exact_structured_output():
    receipt = _receipt()
    parsed = issue_receipts.parse(
        'prospector_app/agent/file-issue --title "t" --body "b"',
        json.dumps(receipt),
        settings.feedback_repo(),
    )
    assert parsed == receipt

    assert issue_receipts.parse(
        "gh issue create --title t", json.dumps(receipt), settings.feedback_repo()
    ) is None
    assert issue_receipts.parse(
        'prospector_app/agent/file-issue --title "t" --body "b"',
        json.dumps({**receipt, "url": receipt["url"] + "9"}),
        settings.feedback_repo(),
    ) is None
    assert issue_receipts.parse(
        'prospector_app/agent/file-issue --title "t" --body "b"',
        json.dumps(receipt), settings.feedback_repo(), is_error=True,
    ) is None


def test_agent_text_is_preserved_without_receipt():
    invented = ("So I filed it: "
                f"https://github.com/{settings.feedback_repo()}/issues/63")
    assert issue_receipts.attach_verified_summary(invented, []) == invented
    negative = "I filed nothing — no receipts, so nothing was created."
    assert issue_receipts.attach_verified_summary(negative, []) == negative


def test_receipt_is_appended_even_when_agent_reports_the_authoritative_url():
    receipt = _receipt()
    text = f"Filed successfully: {receipt['url']}"
    out = issue_receipts.attach_verified_summary(text, [receipt])
    assert out.startswith(text)
    assert "Prospector-verified filing receipt" in out
    assert out.count(receipt["url"]) == 2


def test_receipt_attestation_does_not_rewrite_agent_text():
    receipt = _receipt()
    invented = f"I filed it: https://github.com/{settings.feedback_repo()}/issues/63"
    out = issue_receipts.attach_verified_summary(invented, [receipt])
    assert out.startswith(invented)
    assert receipt["url"] in out
    assert "issues/63" in out


def test_receipt_is_appended_when_model_omits_it():
    receipt = _receipt()
    out = issue_receipts.attach_verified_summary(
        "The filing command succeeded.", [receipt]
    )
    assert "Prospector-verified filing receipt" in out
    assert receipt["url"] in out


def test_stream_preserves_agent_text_without_receipt(
        temp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    monkeypatch.setattr(chat, "_bot_token", lambda: None)
    monkeypatch.setattr(chat, "system_prompt", lambda: "SYS")
    invented = f"I filed it: https://github.com/{settings.feedback_repo()}/issues/63"

    class FakeProc:
        pid = 1
        returncode = 0
        def __init__(self):
            self.stdout = self._gen()
        async def _gen(self):
            yield json.dumps({
                "type": "stream_event",
                "session_id": "sess-A",
                "event": {"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": invented}},
            }).encode() + b"\n"
            yield b'{"type":"result","session_id":"sess-A"}\n'
        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        return FakeProc()

    monkeypatch.setattr(claude_backend.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        return [ev async for ev in chat.stream_chat("file it", pr=7)]

    events = asyncio.run(drive())
    assert events[0] == {"event": "delta", "data": invented}
    assert events[-1]["event"] == "done"
    assert chat.load_thread("pr-7")[-1]["text"] == invented


def test_stream_appends_same_turn_receipt(
        temp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    monkeypatch.setattr(chat, "_bot_token", lambda: None)
    monkeypatch.setattr(chat, "system_prompt", lambda: "SYS")
    receipt = _receipt()
    command = 'prospector_app/agent/file-issue --title "t" --body "b"'
    answer = f"I filed it: {receipt['url']}"

    class FakeProc:
        pid = 2
        returncode = 0
        def __init__(self):
            self.stdout = self._gen()
        async def _gen(self):
            yield json.dumps({
                "type": "assistant", "session_id": "sess-B",
                "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "tool-1", "name": "Bash",
                    "input": {"command": command},
                }]},
            }).encode() + b"\n"
            yield json.dumps({
                "type": "user", "session_id": "sess-B",
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "tool-1",
                    "content": json.dumps(receipt), "is_error": False,
                }]},
            }).encode() + b"\n"
            yield json.dumps({
                "type": "stream_event", "session_id": "sess-B",
                "event": {"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": answer}},
            }).encode() + b"\n"
            yield b'{"type":"result","session_id":"sess-B"}\n'
        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        return FakeProc()

    monkeypatch.setattr(claude_backend.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        return [ev async for ev in chat.stream_chat("file it", pr=8)]

    events = asyncio.run(drive())
    assert events[0] == {"event": "delta", "data": answer}
    assert events[1]["event"] == "delta"
    assert "Prospector-verified filing receipt" in events[1]["data"]
    assert receipt["url"] in events[1]["data"]
    assert events[-1]["event"] == "done"
    saved = chat.load_thread("pr-8")[-1]["text"]
    assert saved == answer + events[1]["data"]
