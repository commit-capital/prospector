"""History-replay instance selection: the deterministic screen that picks the
past bugs the fix lane is scored against, and their grouping by dependency
declarations. Pure functions over caller-supplied candidates and a base clone's
git history — no store, network, agent, or sandbox.

Rules R1–R5 mirror the design spec's "History replay" screen; R6 (the sandbox
oracle) belongs to the run phase, not here.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pipeline import diffpaths, gates
from pipeline.profile import RepoProfile

# R3 size bounds on the landed diff.
MAX_NONTEST_LINES = 400
MAX_FILES = 10

# The shortest abbreviated sha R4 treats as naming the merge commit.
_SHA_MIN_PREFIX = 7

_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "prospector", "GIT_AUTHOR_EMAIL": "prospector@localhost",
            "GIT_COMMITTER_NAME": "prospector", "GIT_COMMITTER_EMAIL": "prospector@localhost"}

_DEP_KEYS = ("dependencies", "devDependencies", "peerDependencies")


@dataclass(frozen=True)
class ClosingPr:
    number: int
    merged: bool
    merge_sha: str
    author: str
    opened_at: datetime


@dataclass(frozen=True)
class Candidate:
    issue: int
    closed_completed: bool
    closing_prs: list[ClosingPr]
    reporter: str
    created_at: datetime
    updated_at: datetime
    report_title: str
    report_body: str


@dataclass(frozen=True)
class Instance:
    issue: int
    pr: int
    merge_sha: str
    landed_diff: str
    test_files: list[str]
    nontest_files: list[str]
    report_title: str
    report_body: str


def _git_env() -> dict[str, str]:
    return {**{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}, **_GIT_ENV}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, timeout=300, env=_git_env())
    return done.stdout


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """True when `ancestor` is reachable from `descendant`. A missing object or
    any other git failure reads as not-an-ancestor, so the candidate fails
    closed."""
    done = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True, text=True, timeout=300, env=_git_env())
    return done.returncode == 0


def _nontest_changed_lines(landed_diff: str) -> int:
    nontest = diffpaths.filter_diff(landed_diff, lambda p: not diffpaths.is_test_path(p))
    return sum(
        1 for line in nontest.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---")))


def _report_references_pr(text: str, pr: int, merge_sha: str) -> bool:
    """True when the report text names the PR. The bare-number match — the number
    flanked by non-digits — covers `#<n>`, a lone `<n>`, and a `/pull/<n>` URL,
    all of which carry the digits as their own token; a hex run of at least
    `_SHA_MIN_PREFIX` that prefixes the merge commit's sha matches the sha form."""
    if re.search(rf"(?<!\d){pr}(?!\d)", text):
        return True
    merge_sha = merge_sha.lower()
    return any(merge_sha.startswith(tok.lower())
               for tok in re.findall(rf"[0-9a-fA-F]{{{_SHA_MIN_PREFIX},40}}", text))


def _is_bot(login: str, profile: RepoProfile, lane_logins: frozenset[str]) -> bool:
    return (login in profile.automation_bots or login.endswith("[bot]")
            or login in lane_logins)


def _is_dep_manifest(path: str, profile: RepoProfile) -> bool:
    """True when `path` is one of `profile`'s dependency manifests. Entries
    without a "/" match the basename (fnmatch); entries with one match the whole
    path in the diffpaths glob dialect."""
    p = diffpaths.normalize_path(path)
    if not p:
        return False
    base = p.rsplit("/", 1)[-1]
    return any(
        diffpaths.matches_glob(p, m) if "/" in m else fnmatch.fnmatch(base, m)
        for m in profile.dependency_manifests)


def screen(candidate: Candidate, *, base_clone: Path, pin_sha: str, profile: RepoProfile,
           lane_logins: frozenset[str] = frozenset()) -> tuple[Instance | None, str | None]:
    """`(instance, None)` when `candidate` passes R1–R5, else `(None, reason)` at
    the first rule it fails. `base_clone` is a checkout whose history holds the
    merge commit and the pin; git reads are hermetic."""
    # R1: closed as completed with exactly one merged closing PR.
    if not candidate.closed_completed:
        return None, "not-closed-completed"
    merged = [pr for pr in candidate.closing_prs if pr.merged]
    if not merged:
        return None, "no-merged-closing-pr"
    if len(merged) > 1:
        return None, "multiple-merged-closing-prs"
    pr = merged[0]

    # R2: the merge commit is an ancestor of the pin; the landed diff is its own.
    if not _is_ancestor(base_clone, pr.merge_sha, pin_sha):
        return None, "merge-not-ancestor-of-pin"
    landed_diff = _git(base_clone, "show", pr.merge_sha)

    # R3: touches a test path and a non-test path, no dependency manifest,
    # bounded non-test lines and files.
    changed = diffpaths.changed_paths(landed_diff)
    test_files = [p for p in changed if diffpaths.is_test_path(p)]
    nontest_files = [p for p in changed if not diffpaths.is_test_path(p)]
    if not test_files:
        return None, "no-test-file"
    if not nontest_files:
        return None, "no-nontest-file"
    if gates.deps_touched(changed):
        return None, "touches-dependency-manifest"
    if _nontest_changed_lines(landed_diff) > MAX_NONTEST_LINES:
        return None, "too-many-nontest-lines"
    if len(changed) > MAX_FILES:
        return None, "too-many-files"

    # R4: the issue predates the PR, was not edited after it opened, and its
    # report never names the PR (discard, never scrub).
    if not candidate.created_at < pr.opened_at:
        return None, "issue-created-after-pr"
    if candidate.updated_at > pr.opened_at:
        return None, "issue-edited-after-pr"
    if _report_references_pr(f"{candidate.report_title}\n{candidate.report_body}",
                             pr.number, pr.merge_sha):
        return None, "report-references-pr"

    # R5: neither the reporter nor the PR author is a bot or a lane identity.
    if (_is_bot(candidate.reporter, profile, lane_logins)
            or _is_bot(pr.author, profile, lane_logins)):
        return None, "bot-or-lane-author"

    return Instance(issue=candidate.issue, pr=pr.number, merge_sha=pr.merge_sha,
                    landed_diff=landed_diff, test_files=test_files,
                    nontest_files=nontest_files, report_title=candidate.report_title,
                    report_body=candidate.report_body), None


def dep_declarations(base_clone: Path, merge_sha: str, profile: RepoProfile) -> dict[str, str]:
    """The merged `dependencies` + `devDependencies` + `peerDependencies` maps of
    every workspace manifest in `base_clone`'s tree at `merge_sha`, read from git
    history. Manifests are found by `profile`'s dependency-manifest globs; only
    name→spec string entries are kept, so lockfiles and non-JSON manifests
    contribute nothing. Sorted by name for a stable, comparable result."""
    tree = _git(base_clone, "ls-tree", "-r", "--name-only", merge_sha).splitlines()
    manifests = sorted(p for p in tree if p and _is_dep_manifest(p, profile))
    merged: dict[str, str] = {}
    for path in manifests:
        try:
            doc = json.loads(_git(base_clone, "show", f"{merge_sha}:{path}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        for key in _DEP_KEYS:
            section = doc.get(key)
            if isinstance(section, dict):
                merged.update((name, spec) for name, spec in section.items()
                              if isinstance(name, str) and isinstance(spec, str))
    return dict(sorted(merged.items()))


def group_by_deps(instances: list[Instance], base_clone: Path, profile: RepoProfile
                  ) -> dict[frozenset[tuple[str, str]], list[Instance]]:
    """`instances` grouped by equal dependency declarations at each instance's
    merge commit, keyed by the frozenset of (name, spec) pairs."""
    groups: dict[frozenset[tuple[str, str]], list[Instance]] = {}
    for inst in instances:
        key = frozenset(dep_declarations(base_clone, inst.merge_sha, profile).items())
        groups.setdefault(key, []).append(inst)
    return groups
