"""Provider boundary for the app's conversational agent: the commands a turn
may run, which each provider renders in its own permission syntax, and the
process lifecycle both CLIs share."""
from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

AGENT_ROOT = Path(__file__).resolve().parents[1] / "agent"
_CACHE = Path(__file__).resolve().parents[1] / "cache"
# The two places a session that grants resubmit may write: `resubmit prepare`
# clones a PR head to RESUBMIT_ROOT/pr-<n>, and the agent authors a --body-file
# in BODY_DIR.
RESUBMIT_ROOT = _CACHE / "resubmit"
BODY_DIR = _CACHE / "chat-bodies"

GH_READS: tuple[tuple[str, ...], ...] = tuple(
    ("gh", *sub.split()) for sub in (
        "pr view", "pr diff", "pr list", "pr checks", "pr status",
        "issue view", "issue list", "search prs", "search issues",
        "search commits", "search repos", "release view", "release list",
        "run view", "run list",
    )
)

# Text filters that narrow a read's output within the one command
# (`gh pr checks … | grep`, `resubmit diff | cut`). Each one only reads its
# input and prints to stdout — none has an option that writes a file or runs a
# program, so a prefix rule on it grants no more than a read. Filters whose
# grammar hides a write (`sed`'s `w`, `awk`'s `print >`/`system()`, `sort`'s
# `-o`/`--compress-program`, `uniq`'s positional output file) are not here: a
# prefix rule cannot reach inside the command to forbid that form, and a
# denylist over it is evadable by reordering flags.
_FILTERS = ("head", "tail", "grep", "wc", "cut", "tr", "jq")

_READ_HELPERS = ("gh-read", "remember", "uncluster", "store-read", "reingest", "file-issue")
# The bot-authenticated helpers, granted with a mintable installation token.
_BOT_HELPERS = ("gh-write", "close-pr", "reopen-pr", "submit-review", "close-issue")
# The confirming operator's push path.
_RESUBMIT_HELPERS = ("resubmit",)


def helpers(can_write: bool, can_resubmit: bool) -> tuple[str, ...]:
    """The curated helpers a turn may run. `can_write` is the bot (a mintable
    installation token); `can_resubmit` is the confirming operator."""
    return (*_READ_HELPERS, *(_BOT_HELPERS if can_write else ()),
            *(_RESUBMIT_HELPERS if can_resubmit else ()))


def command_prefixes(can_write: bool, can_resubmit: bool) -> list[tuple[str, ...]]:
    """Every command prefix a turn may run: the gh reads, the text filters and
    the helpers, by relative and absolute path."""
    return [*GH_READS, *((f,) for f in _FILTERS),
            *(prefix for name in helpers(can_write, can_resubmit)
              for prefix in ((f"prospector_app/agent/{name}",), (str(AGENT_ROOT / name),)))]


@dataclass(frozen=True)
class AgentRequest:
    thread_key: str
    prompt: str
    system_prompt: str
    session_id: str | None
    can_write: bool
    can_resubmit: bool
    cwd: Path
    env: Mapping[str, str]


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolResult:
    command: str
    content: object
    is_error: bool


type AgentEvent = TextDelta | ToolResult


class AgentTurn(Protocol):
    session_id: str | None
    completed: bool

    @property
    def stopped(self) -> bool: ...

    def events(self) -> AsyncIterator[AgentEvent]: ...

    async def close(self) -> None: ...


class AgentBackend(Protocol):
    provider: str

    def readiness(self) -> dict[str, object]: ...

    async def start(self, request: AgentRequest) -> AgentTurn: ...

    def stop(self, thread_key: str) -> bool: ...

    def is_running(self, thread_key: str) -> bool: ...


def terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


class ProcessBackend(AgentBackend):
    """A provider whose turn is one CLI process, registered per thread while it
    runs."""

    def __init__(self) -> None:
        self.running: dict[str, asyncio.subprocess.Process] = {}

    def stop(self, thread_key: str) -> bool:
        proc = self.running.pop(thread_key, None)
        if proc is None:
            return False
        if proc.returncode is None:
            terminate(proc)
        return True

    def is_running(self, thread_key: str) -> bool:
        return thread_key in self.running


class ProcessTurn(AgentTurn):
    def __init__(self, backend: ProcessBackend, request: AgentRequest,
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

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._backend.running.pop(self._request.thread_key, None)
        if self._proc.returncode is None:
            terminate(self._proc)
        await self._proc.wait()
