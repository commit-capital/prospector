"""Codex CLI implementation of the conversational-agent boundary.

Every command the agent runs, and `apply_patch`, is confined by a permissions
profile (`headless_codex.filesystem`): it reads the system directories and the
checkout, writes a private temp directory and, in a session that grants
resubmit, the clones `resubmit prepare` makes and the body-file directory, and
reaches nothing under the operator's home, no credential the deployment names,
no Codex home, and no network.

No execpolicy rule allows a command: an allowed command would run outside the
profile. The host runs the gh reads and the curated helpers a session grants
for the agent instead, through `headless_codex.Broker`: `gh` on the sandbox's
PATH and each helper's path in the prompt name stand-ins, and the host runs
the real tool, holding `gh` to `agent_backend.GH_READS`.

Each chat thread gets an isolated Codex home holding its resume state and the
operator's file-backed login linked in. User configuration and repository
instructions stay out of the prompt.
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
You run in the Codex CLI. Your shell runs in a network-disabled sandbox that
reads this checkout and the system's tools and writes only where the manual
says; the host runs the documented `gh` reads and helpers for you, so run them
with the shell tool exactly as written. The manual's `don't ask mode`,
`/permissions`, and Claude Code modes do not apply: a denial means the action
did not run, and there is no approval prompt to ask the operator for.
""".rstrip()

_OUTPUT_HEADING = "\n# Output\n"

_SHELL_EXCLUDES = [
    "TRIAGE_STORE_URL",
    "DATABASE_URL",
    "*PASSWORD*",
    "ANTHROPIC_*",
]


def _operator_home(env: Mapping[str, str]) -> Path:
    configured = env.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    user_home = env.get("HOME")
    return (Path(user_home).expanduser() if user_home else Path.home()) / ".codex"


def _thread_home(thread_key: str) -> Path:
    digest = hashlib.sha256(thread_key.encode()).hexdigest()[:24]
    return CODEX_CACHE_ROOT / digest


def _sandbox_dir(thread_key: str) -> Path:
    """The thread's stand-ins and temp directory, at a path its resumed turns
    keep, since the first turn's instructions name the stand-ins."""
    return _thread_home(thread_key).with_suffix(".sandbox")


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

    (home / "rules" / "default.rules").unlink(missing_ok=True)
    return home


def _filesystem(cwd: Path, can_resubmit: bool, sandbox: Path) -> dict[str, str]:
    edits = ([str(agent_backend.RESUBMIT_ROOT), str(agent_backend.BODY_DIR)]
             if can_resubmit else [])
    return headless_codex.filesystem([str(cwd)], edits, str(sandbox / "tmp"),
                                     str(CODEX_CACHE_ROOT), str(sandbox / "bin"))


def _stand_in_prompt(text: str, stand_ins: Mapping[str, str]) -> str:
    """`text` with each brokered helper's path, absolute or relative, naming its
    stand-in."""
    for tool, stand_in in stand_ins.items():
        if Path(tool).parent == agent_backend.AGENT_ROOT:
            text = text.replace(tool, stand_in).replace(
                f"prospector_app/agent/{Path(tool).name}", stand_in)
    return text


def _with_codex_context(system_prompt: str) -> str:
    behavior, output_heading, output = system_prompt.partition(_OUTPUT_HEADING)
    if not output_heading:
        return system_prompt + _CODEX_CONTEXT
    return behavior + _CODEX_CONTEXT + output_heading + output


def _gh_read(args: list[str]) -> bool:
    return any(args[:len(read) - 1] == list(read[1:]) for read in agent_backend.GH_READS)


def _flags(system_prompt: str | None, fs: Mapping[str, str]) -> list[str]:
    config = headless_codex.config
    flags = [
        "--json",
        "--ignore-user-config",
        "--strict-config",
        *config("default_permissions", _PROFILE),
        "-c", f"permissions.{_PROFILE}.filesystem={headless_codex.toml_table(fs)}",
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
    def __init__(self, backend: CodexBackend, request: agent_backend.AgentRequest,
                 proc: asyncio.subprocess.Process, broker: headless_codex.Broker) -> None:
        super().__init__(backend, request, proc)
        self._broker = broker
        self._stand_ins = {stand_in: f"prospector_app/agent/{Path(tool).name}"
                           for tool, stand_in in broker.paths.items()
                           if Path(tool).parent == agent_backend.AGENT_ROOT}

    def _command(self, command: str) -> str:
        """The shell command as the manual names it."""
        command = _inner_command(command)
        for stand_in, helper in self._stand_ins.items():
            command = command.replace(stand_in, helper)
        return command

    async def close(self) -> None:
        if self._closed:
            return
        await super().close()
        await asyncio.to_thread(self._broker.__exit__, None, None, None)

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
                            command=self._command(command),
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
        fs = _filesystem(Path.cwd(), can_resubmit=True, sandbox=_sandbox_dir("probe"))
        complaint = config_complaint(_flags(system_prompt="probe", fs=fs))
        if complaint is not None:
            return {"provider": self.provider, "ok": False,
                    "problem": f"Codex cannot start: {complaint}"}
        status = (result.stdout or result.stderr).strip()
        prefix = "Logged in using "
        auth_method = status[len(prefix):] if status.startswith(prefix) else status
        return {"provider": self.provider, "ok": True, "auth_method": auth_method}

    async def start(self, request: agent_backend.AgentRequest) -> CodexTurn:
        home = _prepare_home(request)
        sandbox = _sandbox_dir(request.thread_key)
        shutil.rmtree(sandbox, ignore_errors=True)
        (sandbox / "bin").mkdir(parents=True, mode=0o700)
        (sandbox / "tmp").mkdir(mode=0o700)
        gh = shutil.which("gh", path=request.env.get("PATH"))
        helpers = [str(agent_backend.AGENT_ROOT / name) for name in
                   agent_backend.helpers(request.can_write, request.can_resubmit)]
        broker = headless_codex.Broker(
            [*([gh] if gh else []), *helpers], sandbox / "bin", sandbox / "tmp",
            env=request.env, cwd=str(request.cwd),
            admit={gh: _gh_read} if gh else None,
        ).__enter__()
        fs = _filesystem(request.cwd, request.can_resubmit, sandbox)
        if request.session_id:
            command = [CODEX_BIN, "exec", "resume", *_flags(None, fs), request.session_id]
        else:
            system_prompt = _stand_in_prompt(request.system_prompt, broker.paths)
            command = [CODEX_BIN, "exec", *_flags(system_prompt, fs)]
        env = {**request.env, "CODEX_HOME": str(home), "TMPDIR": str(sandbox / "tmp"),
               "PATH": f"{sandbox / 'bin'}{os.pathsep}{request.env.get('PATH', '')}"}
        try:
            proc = await subproc.spawn(
                [*command, _stand_in_prompt(request.prompt, broker.paths)],
                cwd=request.cwd, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True, env=env,
            )
        except BaseException:
            broker.__exit__(None, None, None)
            raise
        self.running[request.thread_key] = proc
        return CodexTurn(self, request, proc, broker)


CODEX_BACKEND = CodexBackend()
