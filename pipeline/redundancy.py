"""Redundancy signal: which hunks of a PR's diff are already present upstream.

A hunk is *redundant* when its post-image — the retained context plus added
lines, in order — appears as a contiguous block in the current default-branch
copy of the target file: applying the hunk changes nothing that isn't already
there. An identical change on both sides merges cleanly as a no-op, so
`mergeable=CLEAN` cannot surface this; the per-member count in the analyze
bundle can, and the ANALYZE agent reads it as data when weighing whether a
larger diff is genuinely net-new work.

Computed at bundle time against the live tree and never stored: the tree keeps
moving, and freshness stamps facts against a PR's head_sha, which says nothing
about the upstream side.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from urllib.parse import quote

from pipeline import settings

_HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class MasterTree:
    """Cached reads of upstream default-branch file contents via read-only `gh`.

    `read` returns the file's text, "" when the path is absent upstream (an
    addition targeting it is definitively net-new), or None when the content
    could not be determined (auth/network failure, binary or oversized blob).
    After several consecutive hard failures every further read returns None
    without shelling out, so an unauthenticated or offline run costs a handful
    of attempts, not one per file.
    """

    _MAX_FAILURES = 5

    def __init__(self, repo: str = settings.REPO) -> None:
        self.repo = repo
        self._cache: dict[str, str | None] = {}
        self._failures = 0

    def read(self, path: str) -> str | None:
        if path not in self._cache:
            broken = self._failures >= self._MAX_FAILURES
            self._cache[path] = None if broken else self._fetch(path)
        return self._cache[path]

    def _fetch(self, path: str) -> str | None:
        try:
            res = subprocess.run(
                ["gh", "api", f"repos/{self.repo}/contents/{quote(path)}",
                 "-H", "Accept: application/vnd.github.raw+json"],
                capture_output=True, text=True, errors="replace", timeout=60)
        except (subprocess.SubprocessError, OSError):
            self._failures += 1
            return None
        if res.returncode == 0:
            self._failures = 0
            return res.stdout
        if "HTTP 404" in res.stderr:
            self._failures = 0
            return ""
        self._failures += 1
        return None


def compute(diff_text: str, read_file: Callable[[str], str | None]) -> dict | None:
    """Count how many of `diff_text`'s hunks are already present upstream.

    `read_file` maps a repo-relative path to its current default-branch text
    ("" = absent upstream, None = undeterminable). Returns
    {"hunks": <compared>, "redundant": <already present>, "unchecked": <uncomparable>,
    "files": {path: {"hunks": h, "redundant": r}}} with `files` listing only
    paths that have at least one redundant hunk, or None when the diff
    contains no hunks with a post-image.
    """
    per_file = _post_images(diff_text)
    if not per_file:
        return None
    compared = redundant = unchecked = 0
    files: dict[str, dict[str, int]] = {}
    for path, hunks in per_file.items():
        content = read_file(path)
        if content is None:
            unchecked += len(hunks)
            continue
        lines = content.split("\n")
        hits = 0
        for post in hunks:
            compared += 1
            if _contains(lines, post):
                redundant += 1
                hits += 1
        if hits:
            files[path] = {"hunks": len(hunks), "redundant": hits}
    return {"hunks": compared, "redundant": redundant,
            "unchecked": unchecked, "files": files}


def _post_images(diff_text: str) -> dict[str, list[list[str]]]:
    """Parse a unified diff into {target path: [hunk post-images]}. A hunk's
    post-image is its retained-context + added lines in order, prefixes
    stripped. Deleted files (target /dev/null), binary changes (no hunks), and
    hunks with an empty post-image (pure deletion with no surviving context)
    are omitted."""
    out: dict[str, list[list[str]]] = {}
    path: str | None = None
    lines = diff_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            if target.startswith('"') and target.endswith('"'):
                target = target[1:-1]
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@ ") and path is not None:
            post, i = _hunk_post_image(lines, i)
            if post:
                out.setdefault(path, []).append(post)
            continue
        i += 1
    return out


def _hunk_post_image(lines: list[str], i: int) -> tuple[list[str], int]:
    """Post-image of the hunk whose `@@` header is at lines[i]. Consumes body
    lines by the header's old/new counts; returns (post_image, index of the
    first line past the hunk)."""
    m = _HUNK_RE.match(lines[i])
    if m is None:
        return [], i + 1
    old_left = int(m.group(2) if m.group(2) is not None else "1")
    new_left = int(m.group(4) if m.group(4) is not None else "1")
    post: list[str] = []
    i += 1
    while i < len(lines) and (old_left > 0 or new_left > 0):
        body = lines[i]
        tag = body[:1]
        if tag == " " or body == "":  # "" = an empty context line whose space was stripped
            post.append(body[1:])
            old_left -= 1
            new_left -= 1
        elif tag == "-":
            old_left -= 1
        elif tag == "+":
            post.append(body[1:])
            new_left -= 1
        elif tag != "\\":  # "\ No newline at end of file" markers don't count
            break
        i += 1
    return post, i


def _contains(haystack: list[str], needle: list[str]) -> bool:
    """True when `needle` appears as a contiguous run of whole lines in
    `haystack`."""
    return ("\n" + "\n".join(haystack) + "\n").find(
        "\n" + "\n".join(needle) + "\n") != -1
