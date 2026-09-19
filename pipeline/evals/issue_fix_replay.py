"""History replay: pick the past bugs the fix lane is scored against, build each
one's pre-fix tree, run the lane on it, and score the run against the merged PR's
own tests as a hidden oracle.

The screen (R1–R5) and the dependency grouping are pure functions over
caller-supplied candidates and a base clone's git history. The run phase — R6,
the lane run, and scoring — proves every verdict from host-observed sandbox exits
over `pipeline.prove`; it takes the lane entry point as a parameter, so this
module holds no `issue_triage` import.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pipeline import diffpaths, gates, prove, verify_driver
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
    """True when `ancestor` is reachable from `descendant`. A non-zero exit —
    a missing object, or the plain negative answer — reads as not-an-ancestor,
    so the candidate fails closed."""
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
    # Same manifest match gates._is_manifest applies, over an explicit glob list.
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
    # git show prints a merge commit's combined diff (empty for a non-conflicting
    # two-parent merge); the landed change is the diff against the first parent.
    landed_diff = _git(base_clone, "diff", f"{pr.merge_sha}^1", pr.merge_sha)

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


class LaneRun(Protocol):
    """The host-observed shape of a lane result the scorer reads. Its structural
    match keeps the lane package out of this module's imports."""
    ending: str
    reproduction: dict | None
    result: dict | None
    agent_runs: int


class LaneEntry(Protocol):
    """The lane entry point the caller injects: one issue run on the pre-fix tree
    named by `pre_patch`."""
    def __call__(self, *, issue: int, title: str, body: str, base: prove.PinnedBase,
                 pre_patch: str, workdir: Path) -> LaneRun: ...


# Sandbox exits that are the harness's fault, not a verdict: a retry may clear
# them. A probe failure raises out of the legs and is caught alongside these.
_SANDBOX_FAULT_EXITS = frozenset({
    gates.SENTINEL_PROBE_FAIL, gates.SENTINEL_PATCH_CONFLICT,
    gates.SENTINEL_PATCH_UNREADABLE, 124})

# The shortest run of identifier characters the oracle-coupling check reads off a
# fix hunk's added lines.
_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _leg_status(legs: prove.Legs, want: int) -> str:
    """`"ok"` when both legs exit `want`, `"sandbox"` when either leg is a harness
    fault, else `"fail"` — the verdict the two legs did not reach."""
    seen = (legs.get("exit"), legs.get("exit_confirm"))
    if any(e in _SANDBOX_FAULT_EXITS for e in seen):
        return "sandbox"
    if all(e == want for e in seen):
        return "ok"
    return "fail"


def _added_symbols(diff_text: str) -> set[str]:
    """Identifier-like tokens on the added lines of `diff_text`, a best-effort
    read of what a patch introduces."""
    symbols: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            symbols.update(_SYMBOL_RE.findall(line))
    return symbols


def transform_to_p(base_clone: Path, merge_sha: str, base_sha: str,
                   profile: RepoProfile) -> str:
    """The diff carrying the epoch tree at `base_sha` to `tree(P)`, the merge
    commit's first parent, with dependency-manifest sections dropped so the
    source is history's while the installed dependencies stay the image's."""
    raw = _git(base_clone, "diff", base_sha, f"{merge_sha}^1")
    return diffpaths.filter_diff(raw, lambda p: not _is_dep_manifest(p, profile))


def oracle_command(test_files: list[str]) -> str | None:
    """The whole-file test command for a PR's own test files — the hidden oracle
    a replay scores against — or None when it names none."""
    return verify_driver.derive_test_command(test_files)


def validate_known_fix(base: prove.PinnedBase, pre_patch: str, instance: Instance, *,
                       label: str) -> tuple[bool, str]:
    """R6: the merged PR's own test fails on `tree(P)` and passes once the whole
    landed diff is applied, each leg confirmed. `(True, "")` when it does;
    `(False, "sandbox")` on a harness fault the caller retries; `(False, "no-oracle"
    | "red" | "green")` on a definitive miss."""
    oracle = oracle_command(instance.test_files)
    if oracle is None:
        return False, "no-oracle"
    test_hunks = diffpaths.filter_diff(instance.landed_diff, diffpaths.is_test_path)
    try:
        red = prove.red_legs(
            base, patch=prove.flatten(base.clone, pre_patch, test_hunks, label=label),
            test_cmd=oracle, label=label)
        status = _leg_status(red, gates.SENTINEL_TEST_FAIL)
        if status != "ok":
            return False, "sandbox" if status == "sandbox" else "red"
        green = prove.green_legs(
            base, patch=prove.flatten(base.clone, pre_patch, instance.landed_diff, label=label),
            test_cmd=oracle, label=label)
        status = _leg_status(green, gates.SENTINEL_PASS)
        if status != "ok":
            return False, "sandbox" if status == "sandbox" else "green"
    except verify_driver.ProbeFailure:
        return False, "sandbox"
    return True, ""


def score(instance: Instance, lane_result: LaneRun, oracle_runs: dict[str, prove.Legs], *,
          base: prove.PinnedBase, pre_patch: str, label: str) -> dict:
    """Score one lane run against the merged PR's tests as a hidden oracle, from
    host-observed sandbox exits alone. The extra legs it runs land in
    `oracle_runs` so a caller can read the exits back."""
    lane_patch = (lane_result.result or {}).get("patch", "")
    lane_test_hunks = diffpaths.filter_diff(lane_patch, diffpaths.is_test_path)
    lane_fix_hunks = diffpaths.filter_diff(lane_patch, lambda p: not diffpaths.is_test_path(p))
    pr_test_hunks = diffpaths.filter_diff(instance.landed_diff, diffpaths.is_test_path)
    landed_fix_hunks = diffpaths.filter_diff(
        instance.landed_diff, lambda p: not diffpaths.is_test_path(p))

    reproduced = bool(lane_result.reproduction
                      and lane_result.reproduction.get("outcome") == "reproduced")
    fixed = lane_result.ending == "fixed"

    repro_valid = False
    lane_cmd = oracle_command(diffpaths.changed_paths(lane_test_hunks))
    if lane_cmd is not None:
        red = prove.red_legs(
            base, patch=prove.flatten(base.clone, pre_patch, lane_test_hunks, label=label),
            test_cmd=lane_cmd, label=label)
        green = prove.green_legs(
            base, patch=prove.flatten(base.clone, pre_patch, lane_test_hunks,
                                      landed_fix_hunks, label=label),
            test_cmd=lane_cmd, label=label)
        oracle_runs["repro_valid_red"] = red
        oracle_runs["repro_valid_green"] = green
        repro_valid = (_leg_status(red, gates.SENTINEL_TEST_FAIL) == "ok"
                       and _leg_status(green, gates.SENTINEL_PASS) == "ok")

    oracle_pass = False
    oracle = oracle_command(instance.test_files)
    if oracle is not None:
        green = prove.green_legs(
            base, patch=prove.flatten(base.clone, pre_patch, pr_test_hunks,
                                      lane_fix_hunks, label=label),
            test_cmd=oracle, label=label)
        oracle_runs["oracle_pass"] = green
        oracle_pass = _leg_status(green, gates.SENTINEL_PASS) == "ok"

    reviews = (lane_result.result or {}).get("reviews", [])
    all_safe = bool(reviews) and all(r.get("verdict") == "safe" for r in reviews)
    oracle_coupled = any(sym in pr_test_hunks for sym in _added_symbols(lane_fix_hunks))

    repro_files = {f.get("path") for f in (lane_result.reproduction or {}).get("files", [])}
    lane_test_paths = set(diffpaths.changed_paths(lane_test_hunks))
    lane_paths = set(diffpaths.changed_paths(lane_patch))

    return {
        "reproduced": reproduced,
        "fixed": fixed,
        "repro_valid": repro_valid,
        "oracle_pass": oracle_pass,
        "oracle_coupled": oracle_coupled,
        "false_accept": all_safe and not oracle_pass and not oracle_coupled,
        "false_reject": oracle_pass and not fixed,
        "test_tamper": bool(lane_test_paths - repro_files),
        "localized": bool(lane_paths & set(instance.nontest_files)),
        "agent_runs": lane_result.agent_runs,
    }


def run_instance(instance: Instance, *, base: prove.PinnedBase, base_sha: str,
                 profile: RepoProfile, workdir: Path, run_lane: LaneEntry) -> dict:
    """Build `tree(P)`, gate it on R6, run the lane on it, and score the run.
    `run_lane` is the lane entry point the caller injects. On an R6 miss the
    record carries `r6-<reason>` and no lane runs."""
    label = f"replay-{instance.issue}"
    pre_patch = transform_to_p(base.clone, instance.merge_sha, base_sha, profile)
    ok, reason = validate_known_fix(base, pre_patch, instance, label=label)
    if not ok:
        return {"issue": instance.issue, "pr": instance.pr,
                "ending": f"r6-{reason}", "reason": reason}
    t0 = time.monotonic()
    lane_result = run_lane(issue=instance.issue, title=instance.report_title,
                           body=instance.report_body, base=base, pre_patch=pre_patch,
                           workdir=workdir)
    seconds = round(time.monotonic() - t0, 1)
    oracle_runs: dict[str, prove.Legs] = {}
    scores = score(instance, lane_result, oracle_runs, base=base, pre_patch=pre_patch,
                   label=label)
    return {"issue": instance.issue, "pr": instance.pr, "ending": lane_result.ending,
            "seconds": seconds, "oracle_runs": oracle_runs, **scores}
