"""Which checkout is this backend serving.

When several cockpits run at once — one per git worktree — their tabs are
otherwise identical. This reports the branch and worktree directory name so the
frontend can label the tab and let an operator tell instances apart. Git is
shelled out once and cached; the answer can't change without restarting the
process (a worktree is pinned to its checkout).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_cache: dict | None = None


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def instance() -> dict:
    """{"branch", "worktree"} for this checkout (worktree = toplevel dir name)."""
    global _cache
    if _cache is None:
        toplevel = _git("rev-parse", "--show-toplevel")
        _cache = {
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "worktree": Path(toplevel).name if toplevel else None,
        }
    return _cache
