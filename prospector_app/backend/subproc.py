"""Async subprocess spawning for the backend's agent/CLI children.

Every child that streams stdout back into the app is spawned through `spawn`,
so each gets a stdout reader sized for agent output. Provider JSON events can
embed whole file contents in one tool-result line, far past asyncio's 64 KiB
default StreamReader limit. `detach_stdin` keeps the terminal the server was
started from away from every child it starts, spawned here or not.
"""

from __future__ import annotations

import asyncio
import os
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


def detach_stdin() -> None:
    """Point this process's stdin at /dev/null, so no child it starts inherits
    the terminal it was launched from. The `claude` CLI switches a terminal on
    its stdin to raw mode while it runs and leaves it raw when it is killed or
    when its run overlaps another's, and a raw terminal delivers Ctrl-C as a
    keystroke rather than an interrupt."""
    devnull = os.open(os.devnull, os.O_RDONLY)
    if devnull != 0:
        os.dup2(devnull, 0)
        os.close(devnull)
