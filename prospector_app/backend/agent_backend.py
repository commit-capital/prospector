"""Provider boundary for the app's conversational agent."""
from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AgentRequest:
    thread_key: str
    prompt: str
    system_prompt: str
    session_id: str | None
    can_write: bool
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
