"""Claude Code implementation of the conversational-agent boundary.

The CLI runs in safe mode with no settings sources. Its dontAsk permission
boundary advertises repository reads and curated helper scripts, adding the
token-gated write and resubmit paths only for a writable operator session.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

from prospector_app.backend import agent_backend
from prospector_app.backend import subproc

CLAUDE_BIN = shutil.which("claude") or "claude"
AGENT_ROOT = Path(__file__).resolve().parents[1] / "agent"

_GH_ALLOW = [
    "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr list:*)",
    "Bash(gh pr checks:*)", "Bash(gh pr status:*)", "Bash(gh issue view:*)",
    "Bash(gh issue list:*)", "Bash(gh search prs:*)", "Bash(gh search issues:*)",
    "Bash(gh search commits:*)", "Bash(gh release view:*)",
    "Bash(gh release list:*)", "Bash(gh run view:*)", "Bash(gh run list:*)",
]


def _helper_allow(*names: str) -> list[str]:
    return [
        rule
        for name in names
        for rule in (
            f"Bash(prospector_app/agent/{name}:*)",
            f"Bash({AGENT_ROOT / name}:*)",
        )
    ]


_GH_WRITE_ALLOW = _helper_allow("gh-write")

_PR_EXECUTOR_ALLOW = _helper_allow("close-pr", "reopen-pr", "submit-review")

_ISSUE_CLOSE_ALLOW = _helper_allow("close-issue")
_REMEMBER_ALLOW = _helper_allow("remember")
_FILE_ISSUE_ALLOW = _helper_allow("file-issue")
_UNCLUSTER_ALLOW = _helper_allow("uncluster")
_GH_READ_ALLOW = _helper_allow("gh-read")
_GIT_ALLOW = ["Bash(git diff:*)"]
_STORE_READ_ALLOW = _helper_allow("store-read")
_REINGEST_ALLOW = _helper_allow("reingest")
_RESUBMIT_ALLOW = _helper_allow("resubmit")

_DISALLOWED_TOOLS = [
    "Task", "Edit", "Write", "NotebookEdit",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "Skill", "Workflow", "SendMessage", "TeamCreate", "TeamDelete",
    "CronCreate", "CronDelete", "CronList",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "Monitor", "RemoteTrigger", "PushNotification", "ScheduleWakeup",
    "DesignSync", "ToolSearch", "WebFetch", "WebSearch", "AskUserQuestion", "LSP",
]


def isolation_flags(can_write: bool) -> list[str]:
    """Build the complete Claude tool and harness boundary for one turn."""
    allowed = ["Read", "Grep", "Glob", *_GH_ALLOW, *_GH_READ_ALLOW, *_GIT_ALLOW,
               *_REMEMBER_ALLOW, *_UNCLUSTER_ALLOW, *_STORE_READ_ALLOW, *_REINGEST_ALLOW,
               *_FILE_ISSUE_ALLOW]
    disallowed = list(_DISALLOWED_TOOLS)
    if can_write:
        allowed += [*_GH_WRITE_ALLOW, *_PR_EXECUTOR_ALLOW, *_ISSUE_CLOSE_ALLOW,
                    *_RESUBMIT_ALLOW, "Edit", "Write"]
        disallowed = [t for t in disallowed if t not in ("Edit", "Write")]
    return [
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", *disallowed,
        "--permission-mode", "dontAsk",
        "--safe-mode",
        "--setting-sources", "",
    ]


def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


class ClaudeTurn(agent_backend.AgentTurn):
    def __init__(self, backend: ClaudeBackend, request: agent_backend.AgentRequest,
                 proc: asyncio.subprocess.Process) -> None:
        self._backend = backend
        self._request = request
        self._proc = proc
        self._tool_commands: dict[str, str] = {}
        self._text_seen = False
        self._closed = False
        self.session_id: str | None = None
        self.completed = False

    @property
    def stopped(self) -> bool:
        return self._request.thread_key not in self._backend.running

    async def events(self) -> AsyncIterator[agent_backend.AgentEvent]:
        """Normalize Claude stream-json into provider-independent chat events."""
        assert self._proc.stdout is not None
        async for raw in self._proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.session_id = self.session_id or event.get("session_id")
            event_type = event.get("type")
            if event_type == "stream_event":
                stream_event = event.get("event", {})
                if stream_event.get("type") == "content_block_delta":
                    delta = stream_event.get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        self._text_seen = True
                        yield agent_backend.TextDelta(delta["text"])
            elif event_type == "assistant":
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "tool_use" and block.get("name") == "Bash":
                        tool_id = block.get("id")
                        command = (block.get("input") or {}).get("command")
                        if isinstance(tool_id, str) and isinstance(command, str):
                            self._tool_commands[tool_id] = command
                if not self._text_seen:
                    for block in content:
                        if block.get("type") == "text" and block.get("text"):
                            self._text_seen = True
                            yield agent_backend.TextDelta(block["text"])
            elif event_type == "user":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") != "tool_result":
                        continue
                    yield agent_backend.ToolResult(
                        command=self._tool_commands.get(block.get("tool_use_id"), ""),
                        content=block.get("content"),
                        is_error=bool(block.get("is_error")),
                    )
            elif event_type == "result":
                self.completed = True
                break

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._backend.running.pop(self._request.thread_key, None)
        if self._proc.returncode is None:
            _terminate(self._proc)
        await self._proc.wait()


class ClaudeBackend(agent_backend.AgentBackend):
    provider = "claude"

    def __init__(self) -> None:
        self.running: dict[str, asyncio.subprocess.Process] = {}

    def readiness(self) -> dict[str, object]:
        found = shutil.which("claude")
        if found is None:
            return {"provider": self.provider, "ok": False,
                    "problem": "claude CLI not on PATH"}
        try:
            result = subprocess.run(
                [found, "auth", "status", "--json"],
                capture_output=True, text=True, timeout=15,
            )
            status = json.loads(result.stdout)
            logged_in = bool(status.get("loggedIn"))
        except (OSError, subprocess.SubprocessError) as error:
            return {"provider": self.provider, "ok": False,
                    "problem": type(error).__name__}
        except json.JSONDecodeError:
            return {"provider": self.provider, "ok": False,
                    "problem": "unrecognized auth status"}
        if not logged_in:
            return {"provider": self.provider, "ok": False, "problem": "not logged in"}
        return {
            "provider": self.provider,
            "ok": True,
            "auth_method": str(status.get("authMethod") or ""),
            "subscription": str(status.get("subscriptionType") or ""),
        }

    async def start(self, request: agent_backend.AgentRequest) -> ClaudeTurn:
        command = [
            CLAUDE_BIN, "-p", request.prompt,
            *isolation_flags(can_write=request.can_write),
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--append-system-prompt", request.system_prompt,
        ]
        if request.session_id:
            command += ["-r", request.session_id]
        proc = await subproc.spawn(
            command, cwd=request.cwd, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, env=request.env,
        )
        self.running[request.thread_key] = proc
        return ClaudeTurn(self, request, proc)

    def stop(self, thread_key: str) -> bool:
        proc = self.running.pop(thread_key, None)
        if proc is None:
            return False
        if proc.returncode is None:
            _terminate(proc)
        return True

    def is_running(self, thread_key: str) -> bool:
        return thread_key in self.running


CLAUDE_BACKEND = ClaudeBackend()
