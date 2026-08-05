"""Deterministic action-receipt enforcement for embedded chat.

These tests exercise pure parsing/validation only. They never spawn Claude or
make a network call.
"""
import asyncio
import json

from prospector_app.backend import chat
from prospector_app.backend import issue_receipts


def _receipt(number: int = 50) -> dict:
    return {
        "ok": True,
        "kind": "feedback-issue",
        "repo": chat.FEEDBACK_REPO,
        "number": number,
        "url": f"https://github.com/{chat.FEEDBACK_REPO}/issues/{number}",
    }


def test_file_issue_receipt_requires_command_and_exact_structured_output():
    receipt = _receipt()
    parsed = issue_receipts.parse(
        'prospector_app/agent/file-issue --title "t" --body "b"',
        json.dumps(receipt),
        chat.FEEDBACK_REPO,
    )
    assert parsed == receipt

    assert issue_receipts.parse(
        "gh issue create --title t", json.dumps(receipt), chat.FEEDBACK_REPO
    ) is None
    assert issue_receipts.parse(
        'prospector_app/agent/file-issue --title "t" --body "b"',
        json.dumps({**receipt, "url": receipt["url"] + "9"}),
        chat.FEEDBACK_REPO,
    ) is None
    assert issue_receipts.parse(
        'prospector_app/agent/file-issue --title "t" --body "b"',
        json.dumps(receipt), chat.FEEDBACK_REPO, is_error=True,
    ) is None


def test_fabricated_filing_claim_is_withheld_without_receipt():
    invented = ("So I filed it: "
                f"https://github.com/{chat.FEEDBACK_REPO}/issues/63")
    out = issue_receipts.validate(invented, [], chat.FEEDBACK_REPO)
    assert "I did not file an issue" in out
    assert "issues/63" not in out
    assert "only a draft" in out


def test_filing_claim_without_a_url_is_also_withheld():
    out = issue_receipts.validate(
        "I filed it and used the body above.", [], chat.FEEDBACK_REPO
    )
    assert "I did not file an issue" in out
    out = issue_receipts.validate("Filed successfully.", [], chat.FEEDBACK_REPO)
    assert "I did not file an issue" in out


def test_exact_same_turn_receipt_allows_the_authoritative_url():
    receipt = _receipt()
    text = f"Filed successfully: {receipt['url']}"
    assert issue_receipts.validate(text, [receipt], chat.FEEDBACK_REPO) == text


def test_wrong_reported_url_is_replaced_with_same_turn_receipt():
    receipt = _receipt()
    invented = f"I filed it: https://github.com/{chat.FEEDBACK_REPO}/issues/63"
    out = issue_receipts.validate(invented, [receipt], chat.FEEDBACK_REPO)
    assert receipt["url"] in out
    assert "issues/63" not in out
    assert "reported URL did not match" in out


def test_receipt_url_is_appended_when_model_omits_it():
    receipt = _receipt()
    out = issue_receipts.validate(
        "The filing command succeeded.", [receipt], chat.FEEDBACK_REPO
    )
    assert "Verified filing receipt" in out
    assert receipt["url"] in out


def test_stream_replaces_fabricated_claim_without_spawning_agent(
        temp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    monkeypatch.setattr(chat, "_bot_token", lambda: None)
    monkeypatch.setattr(chat, "system_prompt", lambda: "SYS")
    invented = f"I filed it: https://github.com/{chat.FEEDBACK_REPO}/issues/63"

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

    monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        return [ev async for ev in chat.stream_chat("file it", pr=7)]

    events = asyncio.run(drive())
    assert events[0] == {"event": "delta", "data": invented}
    assert events[1]["event"] == "replace"
    assert "I did not file an issue" in events[1]["data"]
    assert "issues/63" not in events[1]["data"]
    assert events[-1]["event"] == "done"
    assert chat.load_thread("pr-7")[-1]["text"] == events[1]["data"]


def test_stream_keeps_claim_with_same_turn_receipt(
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

    monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        return [ev async for ev in chat.stream_chat("file it", pr=8)]

    events = asyncio.run(drive())
    assert events[0] == {"event": "delta", "data": answer}
    assert events[-1]["event"] == "done"
