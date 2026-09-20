"""The clone a lane agent works in: the pinned base tree as a repository with
one commit and no other history, so nothing but the tree reaches the agent and
`git diff HEAD` is exactly what the agent changed."""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from pipeline import diffpaths
from pipeline.wire import VerifyAuthoredFile

# git runs with no user or system configuration and a fixed identity.
_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "prospector", "GIT_AUTHOR_EMAIL": "prospector@localhost",
            "GIT_COMMITTER_NAME": "prospector", "GIT_COMMITTER_EMAIL": "prospector@localhost"}


def _git(worktree: Path, *args: str, input: str | None = None) -> str:
    env = {**{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}, **_GIT_ENV}
    done = subprocess.run(["git", "-C", str(worktree), *args], check=True,
                          capture_output=True, text=True, timeout=300, env=env, input=input)
    return done.stdout


def materialize(base_clone: Path, dest: Path, files: Sequence[VerifyAuthoredFile] = (), *,
                pre_patch: str | None = None) -> Path:
    """A one-commit repository at `dest` holding `base_clone`'s tree, without
    its `.git`, with `pre_patch` applied when given, plus `files`. Returns the
    resolved path."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base_clone, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    if pre_patch is not None:
        try:
            _git(dest, "apply", "--whitespace=nowarn", "-", input=pre_patch)
        except subprocess.CalledProcessError as e:
            raise ValueError(f"pre_patch does not apply: {e.stderr.strip()}") from e
    for f in files:
        target = dest / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["contents"], encoding="utf-8")
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--no-gpg-sign", "-m", "base")
    return Path(os.path.realpath(dest))


def additive_test_edits(worktree: Path, paths: list[str]) -> list[str]:
    """Those of `paths` whose tracked edit only adds lines to a test file — the
    edits a reproduction may make. A removed line, a rename, a delete, or any
    path outside the profile's test conventions is not additive, so a rewrite of
    an existing test can never pass as one."""
    out: list[str] = []
    for path in paths:
        if not diffpaths.is_test_path(path):
            continue
        diff = _git(worktree, "-c", "status.renames=false", "diff", "HEAD", "--", path)
        body = [ln for ln in diff.splitlines()
                if not ln.startswith(("--- ", "+++ ", "diff --git ", "index ", "@@"))]
        if body and all(ln.startswith(("+", " ", "\\")) for ln in body):
            out.append(path)
    return out


def authored_test_diff(worktree: Path, paths: list[str]) -> str:
    """The authored tests as a diff against the worktree's one commit — new
    files and additive edits alike, so a patch carries each as what it is."""
    _git(worktree, "add", "-N", ".")
    return _git(worktree, "diff", "--full-index", "--binary", "HEAD", "--", *paths)


def new_files(worktree: Path) -> tuple[list[str], list[str]]:
    """(the paths the worktree adds, the paths of every other status entry).

    A path is an addition when the one commit does not hold it, which the index
    cannot change: the lane's own check tool stages an agent's new file
    intent-to-add so its diff carries it, and porcelain then reports that file
    as added rather than untracked."""
    # Rename detection off, so every record carries its two-char status prefix
    # and a moved file reads as a delete of the old path plus a new one.
    out = _git(worktree, "-c", "status.renames=false", "status", "--porcelain", "-z",
               "--untracked-files=all")
    committed = set(_git(worktree, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    added: list[str] = []
    other: list[str] = []
    for entry in filter(None, out.split("\0")):
        path = entry[3:]
        (other if path in committed else added).append(path)
    return sorted(added), sorted(other)


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
    """The agent's edits as a diff against the one commit, with full blob ids and
    binary content; new files are marked intent-to-add so they appear."""
    _git(worktree, "add", "-N", ".")
    return _git(worktree, "diff", "--full-index", "--binary", "HEAD")
