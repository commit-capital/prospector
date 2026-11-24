"""Codex CLI implementation of the conversational-agent boundary.

Each chat thread gets an isolated Codex home holding its resume state, the
operator's file-backed login linked in, and an execpolicy that allows
`agent_backend.command_prefixes`. User configuration and repository
instructions stay out of the prompt.

Every other command, and `apply_patch`, runs in a network-disabled sandbox that
reads the whole filesystem. It writes nothing, or, in a session that grants
resubmit, the clones `resubmit prepare` makes, the body-file directory and the
temp directories.

An allowed command runs outside the sandbox, with the network and the
operator's write reach, so the rule alone bounds it. Codex grants that only
while the profile denies no read: denying one pulls every allowed command into
the sandbox, where `gh` and the helpers lose the network. So the sandbox reads
`.env` and the key files, and only the developer instructions keep the agent
from them.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from pipeline import headless_codex
from prospector_app.backend import agent_backend
from prospector_app.backend import subproc

CODEX_BIN = shutil.which("codex") or "codex"
CODEX_CACHE_ROOT = Path(__file__).resolve().parents[1] / "cache" / "codex"


_PROFILE = "chat"

_CODEX_CONTEXT = """

## Codex cockpit
You run in the Codex CLI. Run the documented commands with the shell tool
exactly as written; any other command runs in a network-disabled sandbox. That
sandbox reads the whole filesystem, so the manual's read limits do not hold:
never read `.env` or a private key. Write only where the manual says. Its
`don't ask mode`, `/permissions`, and Claude Code modes do not apply: an
approval or network denial means the action did not run, and there is no
approval prompt to ask the operator for.
""".rstrip()

_OUTPUT_HEADING = "\n# Output\n"

_SHELL_EXCLUDES = [
    "TRIAGE_STORE_URL",
    "DATABASE_URL",
    "*PASSWORD*",
    "ANTHROPIC_*",
]


def isolation_rules(can_write: bool, can_resubmit: bool) -> str:
    return "".join(
        f"prefix_rule(pattern = {json.dumps(list(prefix))}, decision = \"allow\")\n"
        for prefix in agent_backend.command_prefixes(can_write, can_resubmit)
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
        tmp.write(isolation_rules(request.can_write, request.can_resubmit))
        staged = Path(tmp.name)
    staged.chmod(0o600)
    staged.replace(rules_path)
    return home


def _write_profile() -> list[str]:
    """The permissions profile of a session that grants resubmit: read
    everywhere, write to the clones `resubmit prepare` makes, the body-file
    directory, and the temp directories a shell here-document needs. Codex
    parses a `-c` value as TOML, so the table is rendered as one."""
    filesystem = {
        "/": "read",
        os.path.realpath(agent_backend.RESUBMIT_ROOT): "write",
        os.path.realpath(agent_backend.BODY_DIR): "write",
        ":tmpdir": "write",
        "/tmp": "write",
    }
    return [*headless_codex.config("default_permissions", _PROFILE),
            "-c", f"permissions.{_PROFILE}.filesystem={headless_codex.toml_table(filesystem)}"]


def _with_codex_context(system_prompt: str) -> str:
    behavior, output_heading, output = system_prompt.partition(_OUTPUT_HEADING)
    if not output_heading:
        return system_prompt + _CODEX_CONTEXT
    return behavior + _CODEX_CONTEXT + output_heading + output


def _flags(system_prompt: str | None, can_resubmit: bool) -> list[str]:
    config = headless_codex.config
    flags = [
        "--json",
        "--ignore-user-config",
        "--strict-config",
        # Codex refuses `sandbox_mode` beside a permissions profile.
        *(_write_profile() if can_resubmit else config("sandbox_mode", "read-only")),
        *headless_codex.locked_down(),
        *config("include_apps_instructions", False),
        *config("include_collaboration_mode_instructions", False),
        *config("include_environment_context", False),
        *config("include_permissions_instructions", False),
        *config("shell_environment_policy.ignore_default_excludes", False),
        *config("shell_environment_policy.exclude", _SHELL_EXCLUDES),
    ]
    if system_prompt is not None:
        flags += config("developer_instructions", _with_codex_context(system_prompt))
    return flags


_CONFIG_ERROR = "Error loading config.toml"
_ABSENT_THREAD = "00000000-0000-0000-0000-000000000000"


def config_complaint(flags: list[str]) -> str | None:
    """The installed CLI's refusal of these flags, or None when it accepts them.

    Resuming an absent thread under an empty Codex home reaches configuration
    loading and stops there, so the probe needs no login and sends no request.
    """
    with tempfile.TemporaryDirectory() as home:
        try:
            result = subprocess.run(
                [CODEX_BIN, "exec", "resume", *flags, _ABSENT_THREAD, "probe"],
                capture_output=True, text=True, timeout=60,
                stdin=subprocess.DEVNULL, cwd=home,
                env={**os.environ, "CODEX_HOME": home},
            )
        except (OSError, subprocess.SubprocessError) as error:
            return type(error).__name__
    output = result.stdout + result.stderr
    if _CONFIG_ERROR not in output:
        return None
    return " ".join(output[output.index(_CONFIG_ERROR):].split())

def _inner_command(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if (len(parts) >= 3 and Path(parts[0]).name in {"bash", "sh", "zsh"}
            and parts[1] in {"-c", "-lc"}):
        return parts[2]
    return command


class CodexTurn(agent_backend.ProcessTurn):
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


class CodexBackend(agent_backend.ProcessBackend):
    provider = "codex"

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
        complaint = config_complaint(_flags(system_prompt="probe", can_resubmit=True))
        if complaint is not None:
            return {"provider": self.provider, "ok": False,
                    "problem": f"Codex cannot start: {complaint}"}
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
                *_flags(system_prompt=None, can_resubmit=request.can_resubmit),
                request.session_id,
                request.prompt,
            ]
        else:
            command = [
                CODEX_BIN, "exec",
                *_flags(system_prompt=request.system_prompt, can_resubmit=request.can_resubmit),
                request.prompt,
            ]
        proc = await subproc.spawn(
            command, cwd=request.cwd, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, env=env,
        )
        self.running[request.thread_key] = proc
        return CodexTurn(self, request, proc)


CODEX_BACKEND = CodexBackend()
