"""subproc.spawn sizes the stdout reader for agent output: a single stream-json
line carrying a whole tool result (hundreds of KiB) survives line iteration
instead of raising asyncio's default-limit ValueError."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from prospector_app.backend import subproc


def test_line_beyond_default_64k_limit_survives(tmp_path: Path):
    async def run() -> list[int]:
        proc = await subproc.spawn(
            [sys.executable, "-c", "print('x' * 300_000); print('tail')"],
            cwd=tmp_path, stderr=asyncio.subprocess.STDOUT)
        assert proc.stdout is not None
        lengths = [len(raw) async for raw in proc.stdout]
        await proc.wait()
        return lengths

    lengths = asyncio.run(run())
    assert max(lengths) > 64 * 1024
    assert len(lengths) == 2


def test_detach_stdin_gives_children_no_terminal():
    """Run in a child whose stdin is a terminal, so pytest's own fd 0 is left
    alone: after detach_stdin, a process it starts has no terminal on stdin."""
    script = (
        "import os, pty, subprocess, sys\n"
        "master, slave = pty.openpty()\n"
        "os.dup2(slave, 0)\n"
        "from prospector_app.backend import subproc\n"
        "subproc.detach_stdin()\n"
        "subprocess.run([sys.executable, '-c', 'import os; print(os.isatty(0))'])\n")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stderr
