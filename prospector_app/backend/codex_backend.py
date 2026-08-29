"""Codex CLI implementation of the conversational-agent boundary.

Each chat thread gets an isolated Codex home containing its resume state and an
execpolicy allowlist. Codex runs in a network-restricted sandbox that stays
read-only unless the session can mint the bot token; writable sessions may
author the confirmed resubmit edit. Only the same GitHub reads and curated
helper commands exposed to Claude may run outside it. The operator's file-backed
Codex login is linked into the isolated home, while user configuration and
repository instructions stay out of the app agent's prompt.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from prospector_app.backend import agent_backend
from prospector_app.backend import subproc

CODEX_BIN = shutil.which("codex") or "codex"
CODEX_CACHE_ROOT = Path(__file__).resolve().parents[1] / "cache" / "codex"
AGENT_ROOT = Path(__file__).resolve().parents[1] / "agent"


def _helper_prefixes(*names: str) -> tuple[tuple[str, ...], ...]:
    return tuple(
        prefix
        for name in names
        for prefix in (
            (f"prospector_app/agent/{name}",),
            (str(AGENT_ROOT / name),),
        )
    )

_READ_ALLOW: tuple[tuple[str, ...], ...] = (
    ("gh", "pr", "view"),
    ("gh", "pr", "diff"),
    ("gh", "pr", "list"),
    ("gh", "pr", "checks"),
    ("gh", "pr", "status"),
    ("gh", "issue", "view"),
    ("gh", "issue", "list"),
    ("gh", "search", "prs"),
    ("gh", "search", "issues"),
    ("gh", "search", "commits"),
    ("gh", "release", "view"),
    ("gh", "release", "list"),
    ("gh", "run", "view"),
    ("gh", "run", "list"),
    ("git", "diff"),
    *_helper_prefixes("gh-read", "remember", "uncluster", "store-read",
                      "reingest", "file-issue"),
)

_WRITE_ALLOW = _helper_prefixes(
    "gh-write", "close-pr", "reopen-pr", "submit-review", "close-issue", "resubmit",
)

_CODEX_CONTEXT = """

## Codex cockpit
You are running through the Codex CLI. The operating manual's references to
Claude Code's cockpit describe this app's provider-independent boundary. Run
the documented commands with the shell tool exactly as written. Commands not
granted by the cockpit remain inside the network-disabled sandbox. A writable
session changes that sandbox from read-only to workspace-write for the confirmed
resubmit flow. An approval or network denial means the action did not run. Do
not ask the operator for a Codex approval prompt; this embedded surface has none.

In a writable session, use filesystem write tools only for a resubmit the
operator confirmed, and only inside the worktree printed by `resubmit prepare`.
Never edit the primary Prospector checkout.
""".rstrip()

_SHELL_EXCLUDES = [
    "TRIAGE_STORE_URL",
    "DATABASE_URL",
    "*PASSWORD*",
    "ANTHROPIC_*",
]


def isolation_rules(can_write: bool) -> str:
    prefixes = _READ_ALLOW + (_WRITE_ALLOW if can_write else ())
    return "".join(
        f"prefix_rule(pattern = {json.dumps(list(prefix))}, decision = \"allow\")\n"
        for prefix in prefixes
    )


def _operator_home(env: Mapping[str, str]) -> Path:
    configured = env.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    user_home = env.get("HOME")
    return (Path(user_home).expanduser() if user_home else Path.home()) / ".codex"


def _thread_home(thread_key: str) -> Path:
    digest = hashlib.sha256(thread_key.encode()).hexdigest()[:24]
    return CODEX_CACHE_ROOT / digest


def _prepare_home(request: agent_backend.AgentRequest) -> Path:
    source_auth = _operator_home(request.env) / "auth.json"
    if not source_auth.is_file():
        raise RuntimeError("Codex authentication is not file-backed")

    home = _thread_home(request.thread_key)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    auth = home / "auth.json"
    if auth.is_symlink():
        if auth.resolve() != source_auth.resolve():
            raise RuntimeError("isolated Codex authentication points at another login")
    elif auth.exists():
        raise RuntimeError("isolated Codex authentication is not a symlink")
    else:
        auth.symlink_to(source_auth.resolve())

    rules_dir = home / "rules"
    rules_dir.mkdir(mode=0o700, exist_ok=True)
    rules_path = rules_dir / "default.rules"
    with tempfile.NamedTemporaryFile("w", dir=rules_dir, delete=False) as tmp:
        tmp.write(isolation_rules(request.can_write))
        staged = Path(tmp.name)
    staged.chmod(0o600)
    staged.replace(rules_path)
    return home


def _config(key: str, value: object) -> list[str]:
    return ["-c", f"{key}={json.dumps(value)}"]


def _flags(system_prompt: str | None, can_write: bool) -> list[str]:
    flags = [
        "--json",
        "--ignore-user-config",
        "--strict-config",
        *_config("sandbox_mode", "workspace-write" if can_write else "read-only"),
        *_config("approval_policy", "never"),
        *_config("project_doc_max_bytes", 0),
        *_config("include_apps_instructions", False),
        *_config("include_collaboration_mode_instructions", False),
        *_config("include_environment_context", False),
        *_config("include_permissions_instructions", False),
        *_config("allow_login_shell", False),
        *_config("check_for_update_on_startup", False),
        *_config("web_search", "disabled"),
        *_config("orchestrator.skills.enabled", False),
        *_config("orchestrator.mcp.enabled", False),
        *_config("features.multi_agent", False),
        *_config("features.apps", False),
        *_config("features.plugins", False),
        *_config("features.browser_use", False),
        *_config("features.standalone_web_search", False),
        *_config("shell_environment_policy.inherit", "all"),
        *_config("shell_environment_policy.ignore_default_excludes", False),
        *_config("shell_environment_policy.exclude", _SHELL_EXCLUDES),
    ]
    if can_write:
        flags += _config("sandbox_workspace_write.network_access", False)
    if system_prompt is not None:
        flags += _config("developer_instructions", system_prompt + _CODEX_CONTEXT)
    return flags


def _inner_command(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if (len(parts) >= 3 and Path(parts[0]).name in {"bash", "sh", "zsh"}
            and parts[1] in {"-c", "-lc"}):
        return parts[2]
    return command


def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


class CodexTurn(agent_backend.AgentTurn):
    def __init__(self, backend: CodexBackend, request: agent_backend.AgentRequest,
                 proc: asyncio.subprocess.Process) -> None:
        self._backend = backend
        self._request = request
        self._proc = proc
        self._closed = False
        self._text_seen = False
        self.session_id: str | None = None
        self.completed = False

    @property
    def stopped(self) -> bool:
        return self._request.thread_key not in self._backend.running

    async def events(self) -> AsyncIterator[agent_backend.AgentEvent]:
        """Normalize Codex exec JSONL into provider-independent chat events."""
        assert self._proc.stdout is not None
        diagnostic: str | None = None
        async for raw in self._proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                diagnostic = line
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str):
                    self.session_id = thread_id
            elif event_type == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "agent_message" and isinstance(item.get("text"), str):
                    text = item["text"]
                    if text:
                        prefix = "\n\n" if self._text_seen else ""
                        self._text_seen = True
                        yield agent_backend.TextDelta(prefix + text)
                elif item_type == "command_execution":
                    command = item.get("command")
                    output = item.get("aggregated_output")
                    if isinstance(command, str):
                        yield agent_backend.ToolResult(
                            command=_inner_command(command),
                            content=output if isinstance(output, str) else "",
                            is_error=(item.get("status") != "completed"
                                      or item.get("exit_code") != 0),
                        )
            elif event_type == "turn.completed":
                self.completed = True
                break
            elif event_type in {"turn.failed", "error"}:
                diagnostic = None
                error = event.get("error") if event_type == "turn.failed" else event
                message = error.get("message") if isinstance(error, dict) else None
                if isinstance(message, str) and message:
                    prefix = "\n\n" if self._text_seen else ""
                    self._text_seen = True
                    yield agent_backend.TextDelta(f"{prefix}Codex failed: {message}")
                break
        if not self.completed and not self.stopped and diagnostic:
            prefix = "\n\n" if self._text_seen else ""
            yield agent_backend.TextDelta(f"{prefix}Codex failed: {diagnostic}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._backend.running.pop(self._request.thread_key, None)
        if self._proc.returncode is None:
            _terminate(self._proc)
        await self._proc.wait()


class CodexBackend(agent_backend.AgentBackend):
    provider = "codex"

    def __init__(self) -> None:
        self.running: dict[str, asyncio.subprocess.Process] = {}

    def readiness(self) -> dict[str, object]:
        found = shutil.which("codex")
        if found is None:
            return {"provider": self.provider, "ok": False,
                    "problem": "codex CLI not on PATH"}
        try:
            result = subprocess.run(
                [found, "login", "status"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {"provider": self.provider, "ok": False,
                    "problem": type(error).__name__}
        if result.returncode != 0:
            return {"provider": self.provider, "ok": False, "problem": "not logged in"}
        if not (_operator_home(os.environ) / "auth.json").is_file():
            return {"provider": self.provider, "ok": False,
                    "problem": "Codex authentication is not file-backed"}
        status = (result.stdout or result.stderr).strip()
        prefix = "Logged in using "
        auth_method = status[len(prefix):] if status.startswith(prefix) else status
        return {"provider": self.provider, "ok": True, "auth_method": auth_method}

    async def start(self, request: agent_backend.AgentRequest) -> CodexTurn:
        home = _prepare_home(request)
        env = dict(request.env)
        env["CODEX_HOME"] = str(home)
        if request.session_id:
            command = [
                CODEX_BIN, "exec", "resume",
                *_flags(system_prompt=None, can_write=request.can_write),
                request.session_id,
                request.prompt,
            ]
        else:
            command = [
                CODEX_BIN, "exec",
                *_flags(system_prompt=request.system_prompt, can_write=request.can_write),
                request.prompt,
            ]
        proc = await subproc.spawn(
            command, cwd=request.cwd, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, env=env,
        )
        self.running[request.thread_key] = proc
        return CodexTurn(self, request, proc)

    def stop(self, thread_key: str) -> bool:
        proc = self.running.pop(thread_key, None)
        if proc is None:
            return False
        if proc.returncode is None:
            _terminate(proc)
        return True

    def is_running(self, thread_key: str) -> bool:
        return thread_key in self.running


CODEX_BACKEND = CodexBackend()
