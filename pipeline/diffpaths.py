"""Path utilities shared by the pipeline and the app: extracting changed
file paths from a unified diff, the test-file naming convention (the active
profile's `test_paths` rules), and the glob dialect the path policies
(codeowners, risktier) match against.

The test-file convention has a TypeScript twin in
`prospector_app/frontend/src/testPaths.ts`, which consumes the same rules.
"""
from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable

from pipeline import profile


def is_test_path(path: str) -> bool:
    """True if `path` looks like a test file by the active profile's directory
    or filename convention (case-insensitive)."""
    if not path:
        return False
    p = path.strip()
    tp = profile.active().test_paths
    return bool(re.search(tp.dir_pattern, p, re.IGNORECASE)
                or re.search(tp.file_pattern, p, re.IGNORECASE))


def has_tests(paths: list[str]) -> bool:
    """True if any changed path looks like a test file. The corpus-wide has_tests
    signal, derived from a PR's changed-file list."""
    return any(is_test_path(p) for p in paths)


def normalize_path(path: str) -> str:
    """A changed path as the path policies match it: stripped, without a
    leading "./"."""
    p = (path or "").strip()
    return p[2:] if p.startswith("./") else p


def matches_glob(path: str, pattern: str) -> bool:
    """The path-policy glob dialect: `x/**` = the subtree rooted at x;
    `**/x` = x at any depth; anything else is a plain fnmatch."""
    if pattern.endswith("/**"):
        base = pattern[:-3]
        return path == base or path.startswith(base + "/")
    if pattern.startswith("**/"):
        tail = pattern[3:]
        return path == tail or path.endswith("/" + tail)
    return fnmatch.fnmatch(path, pattern)


def changed_paths(text: str) -> list[str]:
    """The list of changed file paths in a unified diff (new path; for deletes,
    the old path)."""
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            tok = line.split(" ")[-1]
            p = tok[2:] if tok.startswith("b/") else tok
        elif line.startswith("+++ "):
            p = line[4:].strip()
            if not p or p == "/dev/null":
                continue
            p = p[2:] if p.startswith("b/") else p
        else:
            continue
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def diff_blocks(text: str) -> list[tuple[str, str]]:
    """A unified diff split into (path, block_text) pairs, one per changed file, in
    order. `path` is the file's `diff --git` new path; `block_text` spans from that
    line up to (not including) the next `diff --git` line, so each block is itself
    a valid patch on its own."""
    blocks: list[tuple[str, str]] = []
    path: str | None = None
    lines: list[str] = []
    for line in (text or "").splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path is not None:
                blocks.append((path, "".join(lines)))
            tok = line.rstrip("\n").split(" ")[-1]
            path = tok[2:] if tok.startswith("b/") else tok
            lines = [line]
        elif path is not None:
            lines.append(line)
    if path is not None:
        blocks.append((path, "".join(lines)))
    return blocks


def filter_diff(text: str, predicate: Callable[[str], bool]) -> str:
    """The subset of a unified diff whose changed files satisfy `predicate(path)` —
    itself a valid patch `git apply` can consume, empty when no file matches."""
    return "".join(block for path, block in diff_blocks(text) if predicate(path))
