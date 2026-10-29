"""Reads e2e artifacts from a `docker cp` tar stream; all input is attacker-controlled."""

from __future__ import annotations

import re
import struct
import tarfile
import unicodedata
import zlib
from dataclasses import dataclass
from typing import IO

MAX_REPORT_BYTES = 20 * 1024 * 1024
MAX_PNG_BYTES = 5 * 1024 * 1024
MAX_PNGS = 200
MAX_ENTRIES = 1000
_MAX_NAME_CHARS = 200

_ROOT = "e2e-out"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_KEY = re.compile(r"^[A-Za-z0-9._/-]+$")
_KEPT_CHUNKS = frozenset({b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"})


@dataclass(frozen=True)
class Artifacts:
    report: bytes | None
    pngs: dict[str, bytes]
    skipped: list[str]


def clean_png(data: bytes) -> bytes | None:
    """The PNG reduced to its critical chunks, or None if it is not a well-formed PNG."""
    if not data.startswith(_PNG_SIGNATURE):
        return None
    kept = [_PNG_SIGNATURE]
    pos, first, idats = len(_PNG_SIGNATURE), True, 0
    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        end = pos + 8 + length + 4
        if end > len(data):
            return None
        ctype = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[end - 4 : end])
        if zlib.crc32(ctype + body) != crc or (first and ctype != b"IHDR"):
            return None
        if (ctype == b"IHDR" and length != 13) or (ctype == b"IEND" and length != 0):
            return None
        first = False
        idats += ctype == b"IDAT"
        if ctype in _KEPT_CHUNKS:
            kept.append(data[pos:end])
        pos = end
        if ctype == b"IEND":
            return b"".join(kept) if pos == len(data) and idats else None
    return None


def _relative_name(name: str) -> str | None:
    """The path under the container's e2e-out root, or None if outside it or unsafe."""
    prefix = _ROOT + "/"
    if not name.startswith(prefix):
        return None
    rel = name[len(prefix) :]
    if not _KEY.match(rel) or any(seg in ("", ".", "..") for seg in rel.split("/")):
        return None
    return rel


def _shown(name: str) -> str:
    """The name made safe to put in a log line."""
    clipped = name[:_MAX_NAME_CHARS]
    return "".join("?" if unicodedata.category(c).startswith("C") else c for c in clipped)


def read_artifacts(stream: IO[bytes]) -> Artifacts:
    report: bytes | None = None
    pngs: dict[str, bytes] = {}
    skipped: list[str] = []
    try:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            seen = 0
            while (member := tar.next()) is not None:
                tar.members = []
                seen += 1
                if seen > MAX_ENTRIES:
                    skipped.append(f"more than {MAX_ENTRIES} entries; the rest were not read")
                    break
                if member.isdir() and (member.name.rstrip("/") == _ROOT or _relative_name(member.name)):
                    continue
                if not member.isreg():
                    skipped.append(f"{_shown(member.name)}: not a regular file")
                    continue
                rel = _relative_name(member.name)
                if rel is None:
                    skipped.append(f"{_shown(member.name)}: outside {_ROOT}/ or unsafe path")
                    continue
                is_report = rel == "report.json"
                if not (is_report or rel.endswith(".png")):
                    skipped.append(f"{_shown(member.name)}: not report.json or a PNG")
                    continue
                cap = MAX_REPORT_BYTES if is_report else MAX_PNG_BYTES
                handle = tar.extractfile(member)
                data = handle.read(cap + 1) if handle else b""
                if len(data) > cap:
                    skipped.append(f"{_shown(member.name)}: over {cap} bytes")
                elif is_report:
                    report = data
                elif len(pngs) >= MAX_PNGS:
                    skipped.append(f"{_shown(member.name)}: more than {MAX_PNGS} PNGs")
                elif (cleaned := clean_png(data)) is None:
                    skipped.append(f"{_shown(member.name)}: not a well-formed PNG")
                else:
                    pngs[rel] = cleaned
    except (tarfile.TarError, EOFError, OSError) as exc:
        skipped.append(f"archive truncated or corrupt: {exc}")
    return Artifacts(report=report, pngs=pngs, skipped=skipped)
