"""The repo-root `.env`, written atomically.

Callers own their own allowlist; this module takes an already-validated mapping
and puts it on disk without disturbing anything else in the file. `.env` holds
the store password and the paths to both credentials, so the file is replaced
whole from a temporary sibling — a failed write leaves the previous file intact
rather than a truncated one.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"


def merge(text: str, updates: dict[str, str]) -> str:
    """`text` with each update applied to its own line, every other line kept
    byte for byte. A key the file does not mention is appended; one it comments
    out is replaced in place, so a commented example becomes the live setting
    rather than a duplicate below it."""
    lines = text.splitlines(keepends=True)
    remaining = dict(updates)
    for i, line in enumerate(lines):
        bare = line.lstrip("#").strip()
        key = bare.split("=", 1)[0].strip() if "=" in bare else ""
        if key not in remaining:
            continue
        ending = "\n" if line.endswith("\n") else ""
        lines[i] = f"{key}={remaining.pop(key)}{ending}"
    if remaining:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n# Written from the app.\n")
        lines.extend(f"{k}={v}\n" for k, v in remaining.items())
    return "".join(lines)


def write(updates: dict[str, str]) -> None:
    """Apply `updates` to `.env` on disk, owner-readable only."""
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    with tempfile.NamedTemporaryFile("w", dir=ENV_PATH.parent, delete=False) as tmp:
        tmp.write(merge(text, updates))
        staged = Path(tmp.name)
    staged.chmod(0o600)
    staged.replace(ENV_PATH)
