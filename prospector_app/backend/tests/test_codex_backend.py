from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from prospector_app.backend import agent_backend
from prospector_app.backend import chat
from prospector_app.backend import codex_backend


class FakeProc:
    pid = 1234

    def __init__(self, lines: list[dict[str, object]], *, running: bool = False) -> None:
        self.returncode = None if running else 0
        self.stdout = self._lines(lines)
        self.waited = False

    async def _lines(self, lines: list[dict[str, object]]):
        for line in lines:
            yield (json.dumps(line) + "\n").encode()

    async def wait(self) -> int:
        self.waited = True
        self.returncode = 0
        return 0


def request(tmp_path: Path, *, session_id: str | None = None,
            can_write: bool = False) -> agent_backend.AgentRequest:
    operator_home = tmp_path / "operator-codex"
    operator_home.mkdir(exist_ok=True)
    (operator_home / "auth.json").write_text("{}")
    return agent_backend.AgentRequest(
        thread_key="pr-12",
        prompt="Is this safe?",
        system_prompt="PROSPECTOR MANUAL",
        session_id=session_id,
        can_write=can_write,
        cwd=tmp_path,
        env={"CODEX_HOME": str(operator_home), "PATH": "/usr/bin"},
    )


def config_value(command: list[str], key: str) -> object:
    prefix = f"{key}="
    for index, arg in enumerate(command):
        if arg == "-c" and command[index + 1].startswith(prefix):
            return json.loads(command[index + 1][len(prefix):])
    raise AssertionError(f"missing Codex config: {key}")


def test_read_only_rules_expose_only_curated_reads_and_local_helpers():
    rules = codex_backend.isolation_rules(can_write=False)

    assert '["gh", "pr", "view"]' in rules
    assert '["gh", "release", "list"]' in rules
    assert '["gh", "run", "list"]' in rules
    assert '["prospector_app/agent/store-read"]' in rules
    assert json.dumps([str(codex_backend.AGENT_ROOT / "store-read")]) in rules
    assert '["prospector_app/agent/file-issue"]' in rules
    assert '["prospector_app/agent/gh-write"]' not in rules
    assert '["prospector_app/agent/resubmit"]' not in rules


def test_chat_registers_and_dispatches_the_codex_backend(monkeypatch):
    monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "codex")
    monkeypatch.setattr(
        codex_backend.CODEX_BACKEND,
        "readiness",
        lambda: {"provider": "codex", "ok": True},
    )

    assert chat._configured_backend() is codex_backend.CODEX_BACKEND
    assert chat.readiness() == {"provider": "codex", "ok": True}


def test_writable_rules_add_only_the_curated_write_helpers():
    read_only = codex_backend.isolation_rules(can_write=False)
    writable = codex_backend.isolation_rules(can_write=True)

    assert writable.startswith(read_only)
    for helper in codex_backend._WRITE_ALLOW:
        assert json.dumps(list(helper)) in writable
    assert '["gh", "pr", "merge"]' not in writable


def test_writable_rules_accept_resolved_absolute_helper_paths():
    rules = codex_backend.isolation_rules(can_write=True)
    for helper in ("gh-write", "close-pr", "reopen-pr", "submit-review", "close-issue", "resubmit"):
        assert json.dumps([str((codex_backend.AGENT_ROOT / helper).resolve())]) in rules


def test_start_isolates_config_and_normalizes_jsonl_events(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_backend, "CODEX_BIN", "/usr/bin/codex")
    monkeypatch.setattr(codex_backend, "CODEX_CACHE_ROOT", tmp_path / "cache")
    captured: dict[str, object] = {}
    proc = FakeProc([
        {"type": "thread.started", "thread_id": "codex-thread"},
        {"type": "item.completed", "item": {
            "id": "one", "type": "agent_message", "text": "Checking now.",
        }},
        {"type": "item.completed", "item": {
            "id": "two", "type": "command_execution",
            "command": "/bin/zsh -c 'prospector_app/agent/store-read pr 12'",
            "aggregated_output": "{\"meta\": {}}\n", "exit_code": 0,
            "status": "completed",
        }},
        {"type": "item.completed", "item": {
            "id": "three", "type": "agent_message", "text": "It is safe.",
        }},
        {"type": "turn.completed", "usage": {}},
    ])

    async def fake_spawn(command, **kwargs):
        captured["command"] = list(command)
        captured["env"] = kwargs["env"]
        return proc

    monkeypatch.setattr(codex_backend.subproc, "spawn", fake_spawn)
    backend = codex_backend.CodexBackend()

    async def drive():
        turn = await backend.start(request(tmp_path))
        events = [event async for event in turn.events()]
        await turn.close()
        return turn, events

    turn, events = asyncio.run(drive())
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["/usr/bin/codex", "exec"]
    assert "resume" not in command
    assert command[-1] == "Is this safe?"
    instructions = config_value(command, "developer_instructions")
    assert isinstance(instructions, str)
    assert "PROSPECTOR MANUAL" in instructions
    assert "only inside the worktree printed by `resubmit prepare`" in instructions
    assert config_value(command, "sandbox_mode") == "read-only"
    assert config_value(command, "approval_policy") == "never"
    assert config_value(command, "project_doc_max_bytes") == 0
    assert "--ignore-user-config" in command
    assert "--ignore-rules" not in command

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    isolated_home = Path(child_env["CODEX_HOME"])
    assert isolated_home != Path(request(tmp_path).env["CODEX_HOME"])
    assert (isolated_home / "auth.json").is_symlink()
    rules = (isolated_home / "rules" / "default.rules").read_text()
    assert '["gh", "pr", "view"]' in rules
    assert '["prospector_app/agent/gh-write"]' not in rules

    assert events == [
        agent_backend.TextDelta("Checking now."),
        agent_backend.ToolResult(
            command="prospector_app/agent/store-read pr 12",
            content="{\"meta\": {}}\n",
            is_error=False,
        ),
        agent_backend.TextDelta("\n\nIt is safe."),
    ]
    assert turn.session_id == "codex-thread"
    assert turn.completed is True
    assert proc.waited is True
    assert backend.is_running("pr-12") is False


def test_resume_uses_the_saved_thread_and_writable_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_backend, "CODEX_BIN", "/usr/bin/codex")
    monkeypatch.setattr(codex_backend, "CODEX_CACHE_ROOT", tmp_path / "cache")
    captured: dict[str, object] = {}
    proc = FakeProc([
        {"type": "thread.started", "thread_id": "saved-thread"},
        {"type": "turn.completed", "usage": {}},
    ])

    async def fake_spawn(command, **kwargs):
        captured["command"] = list(command)
        captured["env"] = kwargs["env"]
        return proc

    monkeypatch.setattr(codex_backend.subproc, "spawn", fake_spawn)
    backend = codex_backend.CodexBackend()

    async def drive():
        turn = await backend.start(request(
            tmp_path, session_id="saved-thread", can_write=True,
        ))
        events = [event async for event in turn.events()]
        await turn.close()
        return events

    assert asyncio.run(drive()) == []
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:3] == ["/usr/bin/codex", "exec", "resume"]
    assert command[-2:] == ["saved-thread", "Is this safe?"]
    assert all("developer_instructions=" not in arg for arg in command)
    assert config_value(command, "sandbox_mode") == "workspace-write"
    assert config_value(command, "sandbox_workspace_write.network_access") is False
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    rules = (Path(child_env["CODEX_HOME"]) / "rules" / "default.rules").read_text()
    assert '["prospector_app/agent/gh-write"]' in rules
    assert '["prospector_app/agent/resubmit"]' in rules


def test_failed_commands_are_normalized_as_error_results(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_backend, "CODEX_CACHE_ROOT", tmp_path / "cache")
    proc = FakeProc([
        {"type": "thread.started", "thread_id": "codex-thread"},
        {"type": "item.completed", "item": {
            "id": "one", "type": "command_execution",
            "command": "/bin/zsh -lc 'gh pr checks 12'",
            "aggregated_output": "network error", "exit_code": 1,
            "status": "failed",
        }},
        {"type": "turn.completed", "usage": {}},
    ])

    async def fake_spawn(command, **kwargs):
        return proc

    monkeypatch.setattr(codex_backend.subproc, "spawn", fake_spawn)
    backend = codex_backend.CodexBackend()

    async def drive():
        turn = await backend.start(request(tmp_path))
        events = [event async for event in turn.events()]
        await turn.close()
        return events

    assert asyncio.run(drive()) == [
        agent_backend.ToolResult(
            command="gh pr checks 12", content="network error", is_error=True,
        ),
    ]


def test_cli_startup_error_is_returned_as_chat_text(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_backend, "CODEX_CACHE_ROOT", tmp_path / "cache")
    proc = FakeProc([])

    async def lines():
        yield b"Error loading config: unknown field\n"

    proc.stdout = lines()

    async def fake_spawn(command, **kwargs):
        return proc

    monkeypatch.setattr(codex_backend.subproc, "spawn", fake_spawn)
    backend = codex_backend.CodexBackend()

    async def drive():
        turn = await backend.start(request(tmp_path))
        events = [event async for event in turn.events()]
        await turn.close()
        return events

    assert asyncio.run(drive()) == [
        agent_backend.TextDelta(
            "Codex failed: Error loading config: unknown field",
        ),
    ]


def test_stop_terminates_the_registered_process(monkeypatch):
    proc = FakeProc([], running=True)
    stopped: list[FakeProc] = []
    monkeypatch.setattr(codex_backend, "_terminate", stopped.append)
    backend = codex_backend.CodexBackend()
    backend.running["pr-12"] = proc

    assert backend.stop("pr-12") is True
    assert stopped == [proc]
    assert backend.stop("pr-12") is False


def test_readiness_reports_the_local_login(tmp_path, monkeypatch):
    operator_home = tmp_path / "operator-codex"
    operator_home.mkdir()
    (operator_home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(operator_home))
    monkeypatch.setattr(codex_backend.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        codex_backend.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="Logged in using ChatGPT\n", stderr="",
        ),
    )

    assert codex_backend.CodexBackend().readiness() == {
        "provider": "codex", "ok": True, "auth_method": "ChatGPT",
    }


def test_readiness_requires_a_binary_login_and_file_auth(tmp_path, monkeypatch):
    backend = codex_backend.CodexBackend()
    monkeypatch.setattr(codex_backend.shutil, "which", lambda name: None)
    assert backend.readiness()["problem"] == "codex CLI not on PATH"

    monkeypatch.setattr(codex_backend.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        codex_backend.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Not logged in",
        ),
    )
    assert backend.readiness()["problem"] == "not logged in"

    monkeypatch.setattr(
        codex_backend.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="Logged in using ChatGPT\n", stderr="",
        ),
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-home"))
    assert backend.readiness()["problem"] == "Codex authentication is not file-backed"


def test_read_only_rules_include_text_filters_and_repo_search():
    rules = codex_backend.isolation_rules(can_write=False)
    for tool in ("head", "tail", "grep", "sed", "awk", "sort", "uniq", "wc", "cut", "tr", "jq"):
        assert json.dumps([tool]) in rules
    assert '["gh", "search", "repos"]' in rules
    assert '["python3"]' not in rules
    assert '["gh", "auth", "status"]' not in rules
