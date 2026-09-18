"""The clone a lane agent works in: the pinned base tree as a repository with
one commit and no other history, so nothing but the tree reaches the agent and
`git diff HEAD` is exactly what the agent changed."""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from pipeline.wire import VerifyAuthoredFile

# git runs with no user or system configuration and a fixed identity.
_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "prospector", "GIT_AUTHOR_EMAIL": "prospector@localhost",
            "GIT_COMMITTER_NAME": "prospector", "GIT_COMMITTER_EMAIL": "prospector@localhost"}


def _git(worktree: Path, *args: str) -> str:
    env = {**{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}, **_GIT_ENV}
    done = subprocess.run(["git", "-C", str(worktree), *args], check=True,
                          capture_output=True, text=True, timeout=300, env=env)
    return done.stdout


def materialize(base_clone: Path, dest: Path,
                files: Sequence[VerifyAuthoredFile] = ()) -> Path:
    """A one-commit repository at `dest` holding `base_clone`'s tree, without
    its `.git`, plus `files`. Returns the resolved path."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base_clone, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    for f in files:
        target = dest / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["contents"], encoding="utf-8")
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--no-gpg-sign", "-m", "base")
    return Path(os.path.realpath(dest))


def new_files(worktree: Path) -> tuple[list[str], list[str]]:
    """(untracked paths, the paths of every other status entry)."""
    # Rename detection off, so every record carries its two-char status prefix
    # and a moved file reads as a delete of the old path plus a new one.
    out = _git(worktree, "-c", "status.renames=false", "status", "--porcelain", "-z",
               "--untracked-files=all")
    untracked: list[str] = []
    other: list[str] = []
    for entry in filter(None, out.split("\0")):
        (untracked if entry.startswith("?? ") else other).append(entry[3:])
    return sorted(untracked), sorted(other)


def read_files(worktree: Path, paths: Sequence[str]
               ) -> tuple[list[VerifyAuthoredFile], str | None]:
    """The named files' contents, or ([], why) at the first one that is not a
    regular UTF-8 file."""
    files: list[VerifyAuthoredFile] = []
    for rel in paths:
        path = worktree / rel
        if path.is_symlink() or not path.is_file():
            return [], f"not a regular file: {rel}"
        try:
            files.append({"path": rel, "contents": path.read_text(encoding="utf-8")})
        except UnicodeDecodeError:
            return [], f"not UTF-8 text: {rel}"
    return files, None


def authored_patch(worktree: Path) -> str:
    """The agent's edits as a diff against the one commit; new files are marked
    intent-to-add so they appear."""
    _git(worktree, "add", "-N", ".")
    return _git(worktree, "diff", "HEAD")
