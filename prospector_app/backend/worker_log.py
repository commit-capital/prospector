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
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import TypedDict
from urllib.parse import unquote, urlsplit

from pipeline import settings, settings_registry

# One file plus one rotated predecessor, so the log never grows past twice this.
ROTATE_BYTES = 10 * 1024 * 1024

# The first line of every file the tee starts: its id, which tells a reader's
# file apart from a later one even when the filesystem reuses its inode.
HEADER = "# prospector worker log "

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
                if not self.file.exists():
                    start_file(self.file)
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


def start_file(file: Path) -> None:
    file.write_text(f"{HEADER}{uuid.uuid4().hex}\n", encoding="utf-8")


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
# The variables whose values no page may show: every secret the settings
# registry names, plus the GitHub tokens `gh` reads.
SECRET_ENV = tuple(s.name for s in settings_registry.SETTINGS if s.kind == "secret") + (
    "GH_TOKEN", "GITHUB_TOKEN")
# Credentials that are not environment values: minted GitHub tokens, private
# key blocks, and a URL's user:password.
SECRET_PATTERNS = (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
                   re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
                   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
                   re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"))


class WorkerLogRead(TypedDict):
    lines: list[str]
    offset: int
    # The file `offset` is in (its header id), so a rotation is told apart
    # from growth; None while there is no log.
    file: str | None
    exists: bool
    # Whether lines were passed over: the reader's file is gone (rotated
    # twice) or was truncated, so this is the current file's tail.
    skipped: bool


def _secrets() -> list[str]:
    values = [v for name in SECRET_ENV if (v := os.environ.get(name))]
    password = urlsplit(os.environ.get("TRIAGE_STORE_URL") or "").password
    if password:
        values += [password, unquote(password)]
    return sorted(set(values), key=len, reverse=True)


def _redact(lines: list[str]) -> list[str]:
    """The lines a page may show: no header line, and no secret."""
    secrets = _secrets()
    out = []
    for line in lines:
        if line.startswith(HEADER):
            continue
        for secret in secrets:
            line = line.replace(secret, "[redacted]")
        for pattern in SECRET_PATTERNS:
            line = pattern.sub("[redacted]", line)
        out.append(line)
    return out


def _cut(chunk: bytes) -> int:
    """Where to cut a read that holds no line end: early enough that a secret
    the cut would split is read whole next time, never inside one this chunk
    holds, and on a UTF-8 character boundary."""
    secrets = [s.encode() for s in _secrets()]
    cut = max(1, len(chunk) - max((len(s) for s in secrets), default=1) + 1)
    for secret in secrets:
        start = chunk.find(secret)
        while start != -1:
            if start < cut < start + len(secret):
                cut = start if start > 0 else start + len(secret)
            start = chunk.find(secret, start + 1)
    while 0 < cut < len(chunk) and chunk[cut] & 0xC0 == 0x80:
        cut -= 1
    return max(cut, 1)


def _read_from(file: Path, start: int, max_bytes: int, *, final: bool = False) -> tuple[list[str], int]:
    """The whole lines from byte `start` and the offset after them. A line
    still being written waits for its newline, unless it alone fills the read;
    a `final` file (rotated, complete) ends its last line at its end."""
    with file.open("rb") as fh:
        fh.seek(start)
        chunk = fh.read(max_bytes)
    end = chunk.rfind(b"\n") + 1
    if end == 0 and chunk:
        if len(chunk) == max_bytes:
            end = _cut(chunk)
        elif final:
            end = len(chunk)
    elif final and len(chunk) < max_bytes:
        end = len(chunk)
    return chunk[:end].decode("utf-8", errors="replace").splitlines(), start + end


def _inode(file: Path) -> str:
    st = file.stat()
    return f"{st.st_dev}:{st.st_ino}"


def _identity(file: Path) -> str:
    """The file's header id; device:inode for a file no tee started."""
    with file.open("rb") as fh:
        first = fh.readline(200).decode("utf-8", errors="replace")
    return first[len(HEADER):].strip() if first.startswith(HEADER) else _inode(file)


def _tail(file: Path, tail_lines: int, max_bytes: int, skipped: bool) -> WorkerLogRead:
    size = file.stat().st_size
    start = max(0, size - max_bytes)
    with file.open("rb") as fh:
        fh.seek(max(0, start - 1))
        chunk = fh.read(size - max(0, start - 1))
    if start > 0:
        # Starting mid-line drops that line's partial head.
        chunk = chunk[1:] if chunk[:1] == b"\n" else chunk[chunk.find(b"\n") + 1:]
    end = chunk.rfind(b"\n") + 1
    lines = chunk[:end].decode("utf-8", errors="replace").splitlines()
    return {"lines": _redact(lines[-tail_lines:]), "offset": size - (len(chunk) - end),
            "file": _identity(file), "exists": True, "skipped": skipped}


def read(after: int | None, file: str | None = None, *, tail_lines: int = 200,
         max_bytes: int = READ_BYTES) -> WorkerLogRead:
    """This machine's worker log for a page following it. With no `after` (or
    no `file`), the last `tail_lines` lines; otherwise the lines written since
    byte `after` of `file`. A reader's file that has rotated to `.log.1` is
    finished first, across as many reads as it takes; one that is gone or was
    truncated answers the current tail, marked `skipped`. Every secret value
    and pattern reads `[redacted]`."""
    current = path()
    if not current.exists():
        return {"lines": [], "offset": 0, "file": None, "exists": False, "skipped": False}
    if after is None or file is None:
        return _tail(current, tail_lines, max_bytes, skipped=False)
    if file == _identity(current):
        if after > current.stat().st_size:
            return _tail(current, tail_lines, max_bytes, skipped=True)
        lines, offset = _read_from(current, after, max_bytes)
        return {"lines": _redact(lines), "offset": offset, "file": file,
                "exists": True, "skipped": False}
    old = current.with_suffix(".log.1")
    if old.exists() and file == _identity(old):
        lines, offset = _read_from(old, after, max_bytes, final=True)
        if offset < old.stat().st_size:
            return {"lines": _redact(lines), "offset": offset, "file": file,
                    "exists": True, "skipped": False}
        more, offset = _read_from(current, 0, max_bytes)
        return {"lines": _redact(lines + more), "offset": offset, "file": _identity(current),
                "exists": True, "skipped": False}
    return _tail(current, tail_lines, max_bytes, skipped=True)
