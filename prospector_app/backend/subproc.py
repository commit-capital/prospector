"""Async subprocess spawning for the backend's agent/CLI children.

Every child that streams stdout back into the app is spawned through `spawn`,
so each gets a stdout reader sized for agent output: headless `claude`
stream-json emits each event as a single line, and a tool-result event embeds
whole file contents (an Edit on a large source file runs to hundreds of KiB on
one line), far past asyncio's 64 KiB default StreamReader limit — which makes
`async for line in proc.stdout` raise ValueError mid-stream.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

STDOUT_LINE_LIMIT = 10 * 1024 * 1024


async def spawn(argv: Sequence[str], *, cwd: Path, stderr: int,
                env: Mapping[str, str] | None = None,
                start_new_session: bool = False) -> asyncio.subprocess.Process:
    """Spawn `argv` with stdout piped through a STDOUT_LINE_LIMIT-sized reader.

    `stderr` takes the asyncio.subprocess constants (STDOUT to interleave,
    DEVNULL to drop). `start_new_session=True` puts the child (and its
    children) in its own process group so a caller can signal the whole tree.
    """
    return await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), limit=STDOUT_LINE_LIMIT,
        stdout=asyncio.subprocess.PIPE, stderr=stderr,
        start_new_session=start_new_session, env=env)
