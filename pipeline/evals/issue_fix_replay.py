"""History replay: pick the past bugs the fix lane is scored against, build each
one's pre-fix tree, run the lane on it, and score the run against the merged PR's
own tests as a hidden oracle.

The screen (R1–R5) and the dependency grouping are pure functions over
caller-supplied candidates and a base clone's git history. The run phase — R6,
the lane run, and scoring — proves every verdict from host-observed sandbox exits
over `pipeline.prove`; it takes the lane entry point as a parameter, so this
module imports nothing from the lane package.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pipeline import diffpaths, gates, gh, profile, prove, settings, store, storekit, verify_driver
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
    landed_diff = _git(base_clone, "diff", "--binary", f"{pr.merge_sha}^1", pr.merge_sha)

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
    detail: str
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
    source is history's while the installed dependencies stay the image's, and
    the sections for files the base's scrub removes or rewrites dropped so the
    diff applies to the scrubbed clone. Binary files carry their full content."""
    raw = _git(base_clone, "diff", "--binary", base_sha, f"{merge_sha}^1")
    return diffpaths.filter_diff(
        raw, lambda p: not _is_dep_manifest(p, profile) and not verify_driver.scrubbed_path(p))


def oracle_command(test_files: list[str]) -> str | None:
    """The whole-file test command for a PR's own test files — the hidden oracle
    a replay scores against — or None when it names none."""
    return verify_driver.derive_test_command(test_files)


def validate_known_fix(base: prove.PinnedBase, pre_patch: str, instance: Instance, *,
                       label: str, legs: dict[str, prove.Legs] | None = None
                       ) -> tuple[bool, str]:
    """R6: the merged PR's own test fails on `tree(P)` and passes once the whole
    landed diff is applied, each leg confirmed. `(True, "")` when it does;
    `(False, "sandbox")` on a harness fault the caller retries; `(False, "no-oracle"
    | "red" | "green")` on a definitive miss. The legs it runs land in `legs`
    under `red` and `green`, and a boot-probe failure under `probe`."""
    legs = {} if legs is None else legs
    oracle = oracle_command(instance.test_files)
    if oracle is None:
        return False, "no-oracle"
    test_hunks = diffpaths.filter_diff(instance.landed_diff, diffpaths.is_test_path)
    try:
        red = legs["red"] = prove.red_legs(
            base, patch=prove.flatten(base.clone, pre_patch, test_hunks, label=label),
            test_cmd=oracle, label=label)
        status = _leg_status(red, gates.SENTINEL_TEST_FAIL)
        if status != "ok":
            return False, "sandbox" if status == "sandbox" else "red"
        green = legs["green"] = prove.green_legs(
            base, patch=prove.flatten(base.clone, pre_patch, instance.landed_diff, label=label),
            test_cmd=oracle, label=label)
        status = _leg_status(green, gates.SENTINEL_PASS)
        if status != "ok":
            return False, "sandbox" if status == "sandbox" else "green"
    except verify_driver.ProbeFailure as e:
        legs["probe"] = {"exit": None, "exit_confirm": None, "output_tail": str(e),
                         "duration_s": 0.0}
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


# The ending recorded for an instance whose run raised.
_CRASH_ENDING = "error"
# Instance endings that are a machine fault rather than a scored verdict: the R6
# sandbox fault, the lane's own faults, and a crash. A --resume re-runs any of them.
_FAULT_ENDINGS = frozenset({"r6-sandbox", "sandbox", "agent-unavailable", "run-failed",
                            "base-compile", _CRASH_ENDING})
# The lane's own fault endings (`_FAULT_ENDINGS` minus the R6 sandbox one and the
# crash): a run that ends in one of these is a machine condition, so it is not scored.
_LANE_FAULT_ENDINGS = _FAULT_ENDINGS - frozenset({"r6-sandbox", _CRASH_ENDING})


def run_instance(instance: Instance, *, base: prove.PinnedBase, base_sha: str,
                 profile: RepoProfile, workdir: Path, run_lane: LaneEntry) -> dict:
    """Build `tree(P)`, gate it on R6, run the lane on it, and score the run.
    `run_lane` is the lane entry point the caller injects. On an R6 miss the
    record carries `r6-<reason>` and no lane runs; a lane fault records the
    ending without scoring."""
    label = f"replay-{instance.issue}"
    pre_patch = transform_to_p(base.clone, instance.merge_sha, base_sha, profile)
    r6_legs: dict[str, prove.Legs] = {}
    t_r6 = time.monotonic()
    ok, reason = validate_known_fix(base, pre_patch, instance, label=label, legs=r6_legs)
    r6_seconds = round(time.monotonic() - t_r6, 1)
    if not ok:
        return {"issue": instance.issue, "pr": instance.pr,
                "ending": f"r6-{reason}", "reason": reason, "r6_seconds": r6_seconds,
                "detail": _legs_detail(r6_legs) or reason}
    t0 = time.monotonic()
    lane_result = run_lane(issue=instance.issue, title=instance.report_title,
                           body=instance.report_body, base=base, pre_patch=pre_patch,
                           workdir=workdir)
    seconds = round(time.monotonic() - t0, 1)
    if lane_result.ending in _LANE_FAULT_ENDINGS:
        # A faulted lane spends none of the oracle and repro_valid sandbox legs.
        return {"issue": instance.issue, "pr": instance.pr, "ending": lane_result.ending,
                "seconds": seconds, "r6_seconds": r6_seconds,
                "agent_runs": lane_result.agent_runs, "detail": lane_result.detail}
    oracle_runs: dict[str, prove.Legs] = {}
    scores = score(instance, lane_result, oracle_runs, base=base, pre_patch=pre_patch,
                   label=label)
    return {"issue": instance.issue, "pr": instance.pr, "ending": lane_result.ending,
            "seconds": seconds, "r6_seconds": r6_seconds, "detail": lane_result.detail,
            "oracle_runs": oracle_runs, **scores}


# The longest detail an instance record carries.
_DETAIL_MAX = 300


def _legs_detail(legs: dict[str, prove.Legs]) -> str:
    """The last leg run, as `<leg> exit <first>/<confirm>: <error excerpt>`, or
    empty when none ran."""
    if not legs:
        return ""
    name, leg = list(legs.items())[-1]
    excerpt = verify_driver.error_excerpt(leg["output_tail"])
    return f"{name} exit {leg['exit']}/{leg['exit_confirm']}: {excerpt}"[:_DETAIL_MAX]


# -- Candidate assembly from the store + live gh reads ------------------------

# How many newest closed issues to assemble by default: the cap bounds the live
# gh fetches a run makes, and recent issues share the current pin's dependencies.
_DEFAULT_CANDIDATES = 300


def _parse_dt(value: str | None) -> datetime | None:
    """A GitHub ISO timestamp as an aware datetime, or None when it is empty or
    unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _gh_pr(number: int) -> dict | None:
    """A merged closing PR read live as the operator: `{merge_sha, author,
    opened_at}`, or None when the PR is unreachable, unmerged, or missing its
    merge commit. `merge_sha` is the commit the merge landed on the default
    branch, whose first-parent diff is the change screening reads."""
    raw = gh.fetch_pr(number)
    if raw is None or not raw.get("merged"):
        return None
    merge_sha = raw.get("merge_commit_sha") or ""
    opened = _parse_dt(raw.get("created_at"))
    if not merge_sha or opened is None:
        return None
    return {"merge_sha": merge_sha, "author": (raw.get("user") or {}).get("login") or "",
            "opened_at": opened}


def candidates_from_store(*, limit: int | None = None) -> list[Candidate]:
    """Every closed issue with a merged closing PR, assembled for screening.

    Issue facts come from the issue store: `updated_at` is the issue's
    `last_edited_at` (its true last edit) so a close never reads as an edit that
    would fail R4. A PR's merge commit, author, and open time, and an issue's
    close reason when the store has none, are read live as the operator. `limit`
    caps enumeration for smoke runs."""
    from issue_triage import issue_links, pr_index
    from issue_triage.issue_store import IssueStore

    issue_store = IssueStore()
    index = pr_index.from_store()
    out: list[Candidate] = []
    # Newest issue first, so `limit` caps the gh fetches on the issues most likely
    # to share the current pin's dependency set.
    for n, iss in sorted(issue_store.all_issues().items(), reverse=True):
        if iss.state != "closed":
            continue
        closers: list[ClosingPr] = []
        for link in issue_links.linked_prs(iss, index.get(n)):
            # Only links that name the PR and the issue together are closer
            # evidence; a still-open PR never closed the issue.
            if link.get("state") == "open" or not issue_links.referenced(link):
                continue
            detail = _gh_pr(int(link["pr"]))
            if detail is None:
                continue
            closers.append(ClosingPr(number=int(link["pr"]), merged=True,
                                     merge_sha=detail["merge_sha"], author=detail["author"],
                                     opened_at=detail["opened_at"]))
        if not closers:
            continue
        created = _parse_dt(iss.created_at)
        edited = _parse_dt(iss.last_edited_at) or created
        if created is None or edited is None:
            continue
        reason = iss.state_reason
        if reason is None:
            raw = gh.gh_json(f"repos/{settings.repo()}/issues/{n}")
            reason = (raw or {}).get("state_reason")
        out.append(Candidate(
            issue=n, closed_completed=reason == "completed", closing_prs=closers,
            reporter=iss.author or "", created_at=created, updated_at=edited,
            report_title=iss.title or "", report_body=iss.body or ""))
        if limit is not None and len(out) >= limit:
            break
    return out


def _run_lane(*, issue: int, title: str, body: str, base: prove.PinnedBase,
              pre_patch: str, workdir: Path) -> LaneRun:
    """The lane entry point `run_instance` injects: one fix run on the pre-fix
    tree. The lane package is imported here so the module top stays lane-free."""
    from issue_triage import fix_lane
    return fix_lane.run(
        fix_lane.LaneSpec(issue=issue, title=title, body=body, base=base,
                          action="fix", pre_patch=pre_patch),
        workdir=workdir)


# -- Screening the corpus into the pin's dependency group ---------------------


def _screen(candidates: list[Candidate], base_clone: Path, base_sha: str, *,
            profile: RepoProfile, lane_logins: frozenset[str]
            ) -> tuple[list[Instance], Counter[str]]:
    """Screen every candidate, returning the passing instances and a count of the
    discards by their first-failed reason."""
    instances: list[Instance] = []
    discards: Counter[str] = Counter()
    for cand in candidates:
        inst, reason = screen(cand, base_clone=base_clone, pin_sha=base_sha,
                              profile=profile, lane_logins=lane_logins)
        if inst is None:
            discards[reason or "unknown"] += 1
        else:
            instances.append(inst)
    return instances, discards


def _pin_key(base_clone: Path, base_sha: str, profile: RepoProfile
             ) -> frozenset[tuple[str, str]]:
    """The dependency declarations at the pinned base, as a group key."""
    return frozenset(dep_declarations(base_clone, base_sha, profile).items())


def _pin_group(groups: dict[frozenset[tuple[str, str]], list[Instance]], *,
               base_clone: Path, base_sha: str, profile: RepoProfile) -> list[Instance]:
    """The instances whose dependencies match the pinned base — the ones the
    machine's image installs faithfully. When none share the pin's exact set, the
    largest group stands in so a pilot still has work."""
    key = _pin_key(base_clone, base_sha, profile)
    if key in groups:
        return groups[key]
    return max(groups.values(), key=len) if groups else []


def select_instances(base: prove.PinnedBase, base_sha: str, *, profile: RepoProfile,
                     lane_logins: frozenset[str],
                     candidate_cap: int = _DEFAULT_CANDIDATES) -> list[Instance]:
    """The pin's dependency group of screened instances, ready to run."""
    instances, _ = _screen(candidates_from_store(limit=candidate_cap), base.clone, base_sha,
                           profile=profile, lane_logins=lane_logins)
    groups = group_by_deps(instances, base.clone, profile)
    return _pin_group(groups, base_clone=base.clone, base_sha=base_sha, profile=profile)


# -- Scorecard table ----------------------------------------------------------

_TABLE_COLUMNS = ("issue", "pr", "ending", "reproduced", "repro_valid", "fixed",
                  "oracle_pass", "false_accept", "seconds", "agent_runs", "detail")
_TABLE_BOOLS = ("reproduced", "repro_valid", "fixed", "oracle_pass", "false_accept")
# The longest detail a table cell shows; the ledger row carries the whole of it.
_TABLE_DETAIL_MAX = 120


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _instance_row(rec: dict) -> str:
    cells = [str(rec.get("issue", "-")), str(rec.get("pr", "-")), _cell(rec.get("ending"))]
    cells += [_cell(rec.get(k)) for k in _TABLE_BOOLS]
    cells += [_cell(rec.get("seconds")), _cell(rec.get("agent_runs"))]
    detail = str(rec.get("detail") or "").replace("\n", " ").replace("|", "\\|")
    cells.append(detail[:_TABLE_DETAIL_MAX] or "-")
    return "| " + " | ".join(cells) + " |"


def _aggregate_row(records: list[dict]) -> str:
    counts = [str(sum(1 for r in records if r.get(k))) for k in _TABLE_BOOLS]
    seconds = sum(r["seconds"] for r in records if isinstance(r.get("seconds"), int | float))
    runs = sum(r["agent_runs"] for r in records if isinstance(r.get("agent_runs"), int))
    # Leading blanks for the pr and ending columns.
    cells = [f"total ({len(records)})", "", ""] + counts + [f"{seconds:.1f}", str(runs), ""]
    return "| " + " | ".join(cells) + " |"


def _render_table(records: list[dict]) -> str:
    header = "| " + " | ".join(_TABLE_COLUMNS) + " |"
    sep = "| " + " | ".join("---" for _ in _TABLE_COLUMNS) + " |"
    rows = [_instance_row(r) for r in records]
    return "\n".join([header, sep, *rows, _aggregate_row(records)]) + "\n"


# -- Ledger -------------------------------------------------------------------

# Instances measured before the average cost per instance is reported.
_COST_SAMPLE = 10

# Newest ledger rows scanned to find this run's recorded instances — an
# index-bounded read, not a full-table scan. A resume within the pilot's window
# sees its rows; a missed older record only re-runs that one instance.
_RESUME_SCAN = 20000


def _is_fault(ending: str | None) -> bool:
    return ending in _FAULT_ENDINGS


def _avg_seconds(records: list[dict]) -> float:
    times = [r["seconds"] for r in records if isinstance(r.get("seconds"), int | float)]
    return sum(times) / len(times) if times else 0.0


def _instance_stats(rec: dict, *, run_id: str, base_sha: str) -> dict:
    stats = {k: v for k, v in rec.items() if k != "oracle_runs"}
    stats.update(run_id=run_id, base_sha=base_sha, host=settings.worker_id())
    return stats


def _run_stats(records: list[dict], *, run_id: str, base_sha: str, concurrency: int,
               limit: int | None) -> dict:
    return {
        "run_id": run_id, "base_sha": base_sha, "host": settings.worker_id(),
        "concurrency": concurrency, "limit": limit, "instances": len(records),
        "reproduced": sum(1 for r in records if r.get("reproduced")),
        "repro_valid": sum(1 for r in records if r.get("repro_valid")),
        "fixed": sum(1 for r in records if r.get("fixed")),
        "oracle_pass": sum(1 for r in records if r.get("oracle_pass")),
        "false_accept": sum(1 for r in records if r.get("false_accept")),
        "false_reject": sum(1 for r in records if r.get("false_reject")),
        "r6_failures": sum(1 for r in records if str(r.get("ending", "")).startswith("r6-")),
        "avg_seconds": round(_avg_seconds(records), 1),
        "agent_runs": sum(r["agent_runs"] for r in records if isinstance(r.get("agent_runs"), int)),
    }


def _progress(line: str) -> None:
    print(f"replay: {line}", file=sys.stderr, flush=True)


def _recorded(pr_store: store.Store, run_id: str) -> dict[int, dict]:
    """The instance stats already in the ledger for `run_id`, keyed by issue —
    the read that makes a run resumable."""
    out: dict[int, dict] = {}
    for rec in pr_store.runs(limit=_RESUME_SCAN):
        if not isinstance(rec, storekit.PhaseRun) or rec.phase != "replay:instance":
            continue
        stats = rec.raw.get("stats") or {}
        if stats.get("run_id") == run_id and isinstance(stats.get("issue"), int):
            out[stats["issue"]] = stats
    return out


# -- Commands -----------------------------------------------------------------


def _date_span(base_clone: Path, members: list[Instance]) -> str:
    dates = [_parse_dt(_merge_committed(base_clone, m.merge_sha)) for m in members]
    got = [d for d in dates if d is not None]
    if not got:
        return "dates unknown"
    return f"{min(got).date()}..{max(got).date()}"


def _merge_committed(base_clone: Path, merge_sha: str) -> str | None:
    try:
        return _git(base_clone, "show", "-s", "--format=%cI", merge_sha).strip()
    except subprocess.CalledProcessError:
        return None


def plan(base: prove.PinnedBase, base_sha: str, *, profile: RepoProfile,
         lane_logins: frozenset[str], candidate_cap: int = _DEFAULT_CANDIDATES) -> None:
    """Assemble and screen the corpus, then print each dependency group's size,
    date span, and whether it matches the machine's pin. No agent or sandbox runs
    — the safety valve to inspect the corpus before a run."""
    instances, discards = _screen(candidates_from_store(limit=candidate_cap), base.clone,
                                  base_sha, profile=profile, lane_logins=lane_logins)
    discarded = sum(discards.values())
    print(f"screened {len(instances) + discarded} candidates: "
          f"{len(instances)} pass, {discarded} discarded", flush=True)
    for reason, count in discards.most_common():
        print(f"  discard {reason}: {count}")
    groups = group_by_deps(instances, base.clone, profile)
    pin_key = _pin_key(base.clone, base_sha, profile)
    print(f"{len(groups)} dependency group(s):")
    for key, members in sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True):
        match = "pin" if key == pin_key else "off-pin"
        print(f"  [{match}] {len(members)} instance(s), {_date_span(base.clone, members)}, "
              f"issues {sorted(m.issue for m in members)}")


def run(base: prove.PinnedBase, base_sha: str, *, profile: RepoProfile,
        lane_logins: frozenset[str], limit: int | None = None, concurrency: int = 2,
        resume: bool = False, run_id: str | None = None,
        candidate_cap: int = _DEFAULT_CANDIDATES) -> int:
    """Run the pin's group through the lane, score each instance, and write one
    `replay:instance` ledger row per instance, a `replay:run` summary, and a
    markdown table. A run is keyed by its base, so re-invoking continues it:
    already-recorded instances are reused, and `resume` re-runs the faulted ones.
    Returns 0 on completion (a failed instance is data), non-zero on a setup
    error."""
    run_id = run_id or base_sha
    instances = select_instances(base, base_sha, profile=profile, lane_logins=lane_logins,
                                 candidate_cap=candidate_cap)
    if not instances:
        print("no candidate instances to replay", file=sys.stderr)
        return 2
    if limit is not None:
        instances = instances[:limit]

    pr_store = store.Store()
    recorded = _recorded(pr_store, run_id)
    results: dict[int, dict] = {}
    todo: list[Instance] = []
    for inst in instances:
        prior = recorded.get(inst.issue)
        if prior is not None and not (resume and _is_fault(prior.get("ending"))):
            results[inst.issue] = prior
        else:
            todo.append(inst)

    out_dir = settings.verify_scratch() / "replay" / run_id
    started = storekit.now()
    completed = 0
    _progress(f"run {run_id[:12]}: {len(todo)} to run, {len(results)} already recorded")

    def one(inst: Instance) -> dict:
        _progress(f"issue {inst.issue} (PR #{inst.pr}): started")
        return run_instance(inst, base=base, base_sha=base_sha, profile=profile,
                            workdir=out_dir / f"issue-{inst.issue}", run_lane=_run_lane)

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {pool.submit(one, inst): inst for inst in todo}
            for fut in as_completed(futures):
                try:
                    rec = fut.result()
                except Exception:
                    # A crashed instance is recorded as an error; the batch goes on.
                    inst = futures[fut]
                    print(f"instance {inst.issue} crashed:", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
                    rec = {"issue": inst.issue, "pr": inst.pr, "ending": _CRASH_ENDING,
                           "seconds": 0.0, "agent_runs": 0}
                results[rec["issue"]] = rec
                _progress(f"issue {rec['issue']}: {rec.get('ending')} "
                          f"({completed + 1}/{len(todo)}, r6 {_cell(rec.get('r6_seconds'))}s, "
                          f"lane {_cell(rec.get('seconds'))}s) {rec.get('detail') or ''}".rstrip())
                pr_store.append_run({
                    "phase": "replay:instance", "started": started,
                    "finished": storekit.now(), "trigger": "cli",
                    "stats": _instance_stats(rec, run_id=run_id, base_sha=base_sha)})
                completed += 1
                if completed == _COST_SAMPLE:
                    ran = [results[i.issue] for i in todo if i.issue in results]
                    print(f"average seconds/instance over first {_COST_SAMPLE}: "
                          f"{_avg_seconds(ran):.1f}", flush=True)
    finished = storekit.now()

    ordered = [results[inst.issue] for inst in instances if inst.issue in results]
    table = _render_table(ordered)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "table.md").write_text(table)
    print(table, flush=True)

    pr_store.append_run({
        "phase": "replay:run", "started": started, "finished": finished, "trigger": "cli",
        "stats": _run_stats(ordered, run_id=run_id, base_sha=base_sha,
                            concurrency=concurrency, limit=limit)})
    return 0


def _lane_logins() -> frozenset[str]:
    """The lane's own GitHub identities, excluded from the corpus so the bot never
    scores its own past work."""
    return frozenset(login for login in (settings.push_login(), settings.bot_login()) if login)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.evals.issue_fix_replay",
        description="Replay the issue-fix lane over past fixed bugs and score it "
                    "against each merged PR's own tests as a hidden oracle.")
    ap.add_argument("command", choices=("plan", "run"),
                    help="plan inspects the corpus; run scores a batch")
    ap.add_argument("--limit", type=int, default=None, help="cap the instances run (pilot: 10)")
    ap.add_argument("--candidates", type=int, default=_DEFAULT_CANDIDATES,
                    help="cap the newest closed issues assembled; a large value is "
                         "network-bound (one live gh read per closed issue's closer)")
    ap.add_argument("--concurrency", type=int, default=2, help="instances kept in flight")
    ap.add_argument("--resume", action="store_true",
                    help="re-run only the faulted instances already recorded for this base")
    ap.add_argument("--base-sha", help="prove against a held base named by its SHA "
                                       "(default: the verify pin)")
    ap.add_argument("--tier", type=int, default=0,
                    help="the held base's risk tier (with --base-sha)")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        base = (prove.held(args.base_sha, args.tier) if args.base_sha
                else prove.pinned(store.Store()))
    except prove.NoBase as e:
        print(str(e), file=sys.stderr)
        return 2
    active = profile.active()
    logins = _lane_logins()
    if args.command == "plan":
        plan(base, base.sha, profile=active, lane_logins=logins, candidate_cap=args.candidates)
        return 0
    return run(base, base.sha, profile=active, lane_logins=logins, limit=args.limit,
               concurrency=args.concurrency, resume=args.resume, candidate_cap=args.candidates)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
