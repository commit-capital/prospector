"""A durable copy of the worker's stdout.

The worker lanes report by printing to the backend's stdout, which is a
terminal or a service log outside this process's control. An operator
diagnosing a machine that has been failing for days needs more than the
terminal's scrollback, so every lane mirrors the process's stdout to a file
under the verify scratch directory, which the Health & queues page follows
(`read`).
"""
from __future__ import annotations

import io
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

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



# The most one read returns, so a page that fell far behind catches up in steps.
READ_BYTES = 256 * 1024
# The deployment values a traceback may print that no page may show.
SECRET_ENV = ("TRIAGE_STORE_URL", "PROSPECTOR_FEEDBACK_TOKEN", "TRIAGE_SLACK_WEBHOOK_URL",
              "GH_TOKEN", "GITHUB_TOKEN")


def _secrets() -> list[str]:
    values = [v for name in SECRET_ENV if (v := os.environ.get(name))]
    password = urlsplit(os.environ.get("TRIAGE_STORE_URL") or "").password
    if password:
        values.append(password)
    return sorted(values, key=len, reverse=True)


def _redact(lines: list[str]) -> list[str]:
    secrets = _secrets()
    out = []
    for line in lines:
        for secret in secrets:
            line = line.replace(secret, "[redacted]")
        out.append(line)
    return out


def _complete(chunk: bytes, full: bool) -> tuple[list[str], int]:
    """The whole lines in `chunk` and the bytes they span. A line still being
    written waits for its newline, unless it alone fills a `full` read."""
    end = chunk.rfind(b"\n") + 1
    if end == 0 and full:
        end = len(chunk)
    text = chunk[:end].decode("utf-8", errors="replace")
    return text.splitlines(), end


def _read_from(file: Path, start: int, max_bytes: int) -> tuple[list[str], int]:
    with file.open("rb") as fh:
        fh.seek(start)
        chunk = fh.read(max_bytes)
    lines, used = _complete(chunk, len(chunk) == max_bytes)
    return lines, start + used


def read(after: int | None, *, tail_lines: int = 200, max_bytes: int = READ_BYTES) -> dict:
    """This machine's worker log for a page following it: with no `after`, the
    last `tail_lines` lines; otherwise the lines written since byte `after`.
    `offset` is where the next read starts. A file shorter than `after` has
    rotated, so the rest of its predecessor comes first. Every SECRET_ENV value
    reads `[redacted]`."""
    file = path()
    if not file.exists():
        return {"lines": [], "offset": 0, "exists": False}
    size = file.stat().st_size
    if after is None:
        start = max(0, size - max_bytes)
        lines, offset = _read_from(file, start, max_bytes)
        if start > 0:
            lines = lines[1:]
        return {"lines": _redact(lines[-tail_lines:]), "offset": offset, "exists": True}
    lines: list[str] = []
    if after > size:
        old = file.with_suffix(".log.1")
        if old.exists():
            lines, _ = _read_from(old, after, max_bytes)
        after = 0
    more, offset = _read_from(file, after, max_bytes)
    return {"lines": _redact(lines + more), "offset": offset, "exists": True}
