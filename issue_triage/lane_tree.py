"""The clone a lane agent works in: the pinned base tree as a repository with
one commit and no other history, so nothing but the tree reaches the agent and
`git diff HEAD` is exactly what the agent changed."""
from __future__ import annotations

import difflib
import os
import re
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


def apply_patch(worktree: Path, patch: str) -> None:
    """Apply `patch` to `worktree`'s files. Raises ValueError when it does not apply."""
    if not patch.strip():
        return
    try:
        _git(worktree, "apply", "--whitespace=nowarn", "-", input=patch)
    except subprocess.CalledProcessError as e:
        raise ValueError(f"the patch does not apply: {e.stderr.strip()}") from e


def additive_test_edits(worktree: Path, paths: list[str]) -> list[str]:
    """Those of `paths` whose tracked edit only adds to a test file — the edits
    a reproduction may make. An edit adds when it only adds lines, or when the
    only lines it removes are import statements it widens: the same statement
    from the same module, every name it imported still there. Any other removed
    line, a rename, a delete, a mode change, or any path outside the profile's
    test conventions is not additive, so a rewrite of an existing test can
    never pass as one."""
    out: list[str] = []
    for path in paths:
        if not diffpaths.is_test_path(path):
            continue
        diff = _git(worktree, "-c", "status.renames=false", "diff", "HEAD", "--", path)
        body = [ln for ln in diff.splitlines()
                if not ln.startswith(("--- ", "+++ ", "diff --git ", "index ", "@@"))]
        if not body or not all(ln.startswith(("+", "-", " ", "\\")) for ln in body):
            continue
        if (all(ln.startswith(("+", " ", "\\")) for ln in body)
                or _only_widens_imports(worktree, path)):
            out.append(path)
    return out


# One import or re-export statement, whitespace collapsed: JS/TS
# `import|export [type] <clause> from "<module>"` and Python
# `from <module> import <names>`.
_JS_STATEMENT = re.compile(r"""(import|export)( type)? (.+?) from (["'])([^"']+)\4 ?;?""")
_PY_STATEMENT = re.compile(r"from ([\w.]+) import (.+)")
_JS_NAME = re.compile(r"(type )?[\w$]+( as [\w$]+)?")
_PY_NAME = re.compile(r"\w+( as \w+)?")
_STATEMENT_START = re.compile(r"\s*(import|export|from)\b")
_MAX_STATEMENT_LINES = 200

# ("import", what must stay the same, the names it imports) for a statement
# this reads, ("line", (the line,), no names) for anything else.
_Unit = tuple[str, tuple[str, ...], frozenset[str]]


def _names(inner: str, name: re.Pattern[str]) -> frozenset[str] | None:
    items = [i.strip() for i in inner.split(",")]
    items = [i for i in items if i]
    if not items or not all(name.fullmatch(i) for i in items):
        return None
    return frozenset(items)


def _parse_import(text: str) -> _Unit | None:
    """`text` as an import statement whose names a widening may add to, or None
    when it is not one this can read (a comment in it, a side-effect import, a
    dynamic one, anything unusual) — so an edit to it stays a rewrite."""
    flat = " ".join(text.split())
    if "//" in flat or "/*" in flat or "#" in flat:
        return None
    if m := _JS_STATEMENT.fullmatch(flat):
        keyword, typed, clause, module = m.group(1), m.group(2) or "", m.group(3), m.group(5)
        head, brace, inner = clause.partition("{")
        if brace and inner.endswith("}"):
            head = head.strip().removesuffix(",").strip()
            if head and not re.fullmatch(r"[\w$]+", head):
                return None
            names = _names(inner[:-1], _JS_NAME)
            if names is None:
                return None
            return ("import", ("js", keyword, typed, module, head), names)
        # A default or namespace import names nothing to widen; it must stay as it is.
        return ("import", ("js", keyword, typed, module, clause), frozenset())
    if m := _PY_STATEMENT.fullmatch(flat):
        module, rest = m.group(1), m.group(2).strip()
        if rest.startswith("("):
            if not rest.endswith(")"):
                return None
            rest = rest[1:-1]
        if rest.strip() == "*":
            return ("import", ("py", module, "*"), frozenset())
        names = _names(rest, _PY_NAME)
        return None if names is None else ("import", ("py", module, ""), names)
    return None


def _units(text: str) -> list[_Unit]:
    """`text` as a sequence of units: each import statement it can read, however
    many lines it spans, is one; every other line is its own."""
    lines = text.splitlines()
    units: list[_Unit] = []
    i = 0
    while i < len(lines):
        if _STATEMENT_START.match(lines[i]):
            for j in range(i, min(i + _MAX_STATEMENT_LINES, len(lines))):
                if (parsed := _parse_import("\n".join(lines[i:j + 1]))) is not None:
                    units.append(parsed)
                    i = j + 1
                    break
            else:
                units.append(("line", (lines[i],), frozenset()))
                i += 1
            continue
        units.append(("line", (lines[i],), frozenset()))
        i += 1
    return units


def _only_widens_imports(worktree: Path, path: str) -> bool:
    """True when every unit the edit to `path` removes is an import statement
    it replaces with one from the same module importing at least the same
    names — so nothing the test ran on is taken away or changed."""
    target = worktree / path
    if target.is_symlink() or not target.is_file():
        return False
    try:
        before = _git(worktree, "show", f"HEAD:{path}")
        after = target.read_text(encoding="utf-8")
    except (subprocess.CalledProcessError, UnicodeDecodeError):
        return False
    old, new = _units(before), _units(after)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "insert"):
            continue
        added = list(new[j1:j2])
        for unit in old[i1:i2]:
            if unit[0] != "import":
                return False
            k = next((k for k, a in enumerate(added)
                      if a[0] == "import" and a[1] == unit[1] and unit[2] <= a[2]), None)
            if k is None:
                return False
            del added[k]
    return True


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


def committed_texts(worktree: Path, paths: Sequence[str]) -> dict[str, str]:
    """The one commit's copy of each of `paths` it holds as text, by path."""
    out: dict[str, str] = {}
    for path in paths:
        try:
            out[path] = _git(worktree, "show", f"HEAD:{path}")
        except (subprocess.CalledProcessError, UnicodeDecodeError):
            continue
    return out


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
