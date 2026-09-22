"""A durable copy of the worker's stdout.

Both workers report by printing to the backend's stdout, which is a terminal
or a service log outside this process's control. An operator diagnosing a
machine that has been failing for days needs more than the terminal's
scrollback, so the workers also mirror every line to a file under the verify
scratch directory.
"""
from __future__ import annotations

import io
import sys
import threading
from pathlib import Path

from pipeline import settings

# One file plus one rotated predecessor, so the log never grows past twice this.
ROTATE_BYTES = 10 * 1024 * 1024

_lock = threading.Lock()
_installed: _Tee | None = None


def path() -> Path:
    return settings.verify_scratch() / "logs" / f"worker-{settings.worker_id()}.log"


class _Tee(io.TextIOBase):
    """Writes to the wrapped stream and appends to the log file."""

    def __init__(self, wrapped: io.TextIOBase, file: Path) -> None:
        self.wrapped = wrapped
        self.file = file

    def write(self, s: str) -> int:
        n = self.wrapped.write(s)
        try:
            with _lock:
                if self.file.exists() and self.file.stat().st_size > ROTATE_BYTES:
                    self.file.replace(self.file.with_suffix(".log.1"))
                with self.file.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(s)
        except OSError:
            pass
        return n

    def flush(self) -> None:
        self.wrapped.flush()

    def isatty(self) -> bool:
        return self.wrapped.isatty()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self.wrapped, "encoding", "utf-8")


def install() -> Path | None:
    """Start mirroring stdout to the log file. Idempotent; None when the
    directory cannot be created, in which case stdout is left alone."""
    global _installed
    with _lock:
        if _installed is not None:
            return _installed.file
        target = path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        _installed = _Tee(sys.stdout, target)  # type: ignore[arg-type]
        sys.stdout = _installed
        return target

