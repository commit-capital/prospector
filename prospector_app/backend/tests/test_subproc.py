"""subproc.spawn sizes the stdout reader for agent output: a single stream-json
line carrying a whole tool result (hundreds of KiB) survives line iteration
instead of raising asyncio's default-limit ValueError."""

from __future__ import annotations

import asyncio
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
