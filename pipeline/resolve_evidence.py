"""Evidence for judging an agent's merge-conflict resolution.

Read-only helpers over the kept merge worktree — whose HEAD is the merge
commit, `HEAD^1` the PR's head and `HEAD^2` the merged-in base — plus the
store's knowledge of the PR. The autopush reviewers consume all of it as
prompt text; nothing here calls GitHub.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import diffpaths

if TYPE_CHECKING:
    from pipeline.model import Pr

# Commits shown per side per conflicted path.
LOG_LIMIT = 8

# Test files handed to the sandbox per resolve.
MAX_RELATED_TESTS = 10

GIT_TIMEOUT_SECONDS = 120


def _git(worktree: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", worktree, *args], capture_output=True,
                       text=True, timeout=GIT_TIMEOUT_SECONDS)
    return r.stdout if r.returncode == 0 else ""


def history(worktree: str, conflict_paths: list[str]) -> str:
    """Per conflicted path, the commits unique to each side of the merge that
    touched it — squash-merge subjects carry the PR/issue numbers behind each
    side's hunks."""
    blocks: list[str] = []
    for path in conflict_paths:
        pr_side = _git(worktree, "log", "--oneline", f"-n{LOG_LIMIT}",
                       "HEAD^2..HEAD^1", "--", path).strip()
        base_side = _git(worktree, "log", "--oneline", f"-n{LOG_LIMIT}",
                         "HEAD^1..HEAD^2", "--", path).strip()
        blocks.append(
            f"{path}:\n"
            f"  commits on this PR's side:\n{_indent(pr_side)}\n"
            f"  commits on the base side:\n{_indent(base_side)}")
    return "\n".join(blocks)


def _indent(text: str) -> str:
    if not text:
        return "    (none)"
    return "\n".join("    " + line for line in text.splitlines())


def related_tests(worktree: str, conflict_paths: list[str]) -> list[str]:
    """Repo-relative test files related to the conflicted paths: conflicted
    paths that are themselves tests, then test files whose name or text
    references a conflicted file's stem, capped at MAX_RELATED_TESTS."""
    picked: list[str] = []
    stems = []
    for p in conflict_paths:
        norm = diffpaths.normalize_path(p)
        if not norm:
            continue
        if diffpaths.is_test_path(norm):
            picked.append(norm)
        else:
            stems.append(Path(norm).stem)
    all_tests = [f for f in _git(worktree, "ls-files").splitlines()
                 if diffpaths.is_test_path(f) and f not in picked]
    for f in all_tests:
        if any(stem and stem in Path(f).stem for stem in stems):
            picked.append(f)
    for stem in stems:
        if not stem:
            continue
        hits = _git(worktree, "grep", "-l", "--", stem, "--", *all_tests)
        picked.extend(f for f in hits.splitlines() if f and f not in picked)
    return picked[:MAX_RELATED_TESTS]


def store_context(rec: Pr) -> str:
    """The store's one-paragraph picture of the PR: its summary and linked
    issues. Empty when the store holds neither."""
    parts: list[str] = []
    summary = rec.rec.get("summary") or {}
    for key in ("one_liner", "primary_change"):
        val = str(summary.get(key) or "").strip()
        if val and val not in parts:
            parts.append(val)
    lines = [". ".join(parts)] if parts else []
    for link in rec.linked_issues:
        num = link.get("issue")
        if num is None:
            continue
        title = str(link.get("title") or "").strip()
        lines.append(f"linked issue #{num}" + (f": {title}" if title else ""))
    return "\n".join(lines)
