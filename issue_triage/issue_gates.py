"""The ONE issue-side policy module: close-as-dup, derived cluster state, and the
issue-fix lane's gates.

close_dup_allowed is the pipeline's static auto-recommend (drives the dup
worklist); close_dup_eligibility adds live upstream checks that the duplicate and
is still open, and is the app/executor's pre-write gate — the issue-side analog of
gates.py's merge_allowed vs merge_eligibility.
issue_cluster_state is the derived board chip, computed on read, never stored.
reproduction_outcome names a reproduction attempt's result from the host's red
exits and the judge's ratings; fix_patch_regate and fix_proof_bar hold an
agent-authored fix to what it may touch and to host-observed proof plus two
refuting reviews.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from issue_triage.issue_freshness import is_current
from pipeline import author_fix, diffpaths, gates, risktier, threats

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue, IssueCluster

def close_dup_allowed(issue: Issue, cluster: IssueCluster | None,
                      today: str | None = None) -> tuple[bool, str]:
    """Static gate (pipeline auto-recommend): an open, confirmed, fresh, curated
    duplicate pointing at a canonical other than itself."""
    if issue.state is not None and issue.state != "open":
        return False, f"issue is already {issue.state}"
    if cluster is not None and cluster.needs_review:
        return False, "cluster flagged needs-review — curate (split) before closing"
    if not is_current(issue, "analysis"):
        return False, "analysis missing or stale — re-run ANALYZE"
    if issue.disposition != "close-dup":
        return False, f"disposition is {issue.disposition}, not close-dup"
    canon = issue.canonical
    if not canon:
        return False, "no canonical issue recorded"
    if canon == issue.number:
        return False, "canonical is the issue itself"
    if cluster is None or not (cluster.curation or {}).get("confirmed"):
        return False, "cluster duplicates not confirmed (curation pending)"
    return True, "confirmed duplicate — ready to close"


def close_dup_eligibility(issue: Issue, cluster: IssueCluster | None,
                          issues: dict[int, Issue] | None = None,
                          live_state: Callable[[int], str | None] | None = None,
                          today: str | None = None,
                          ) -> tuple[bool, str]:
    """Executor's pre-write gate: close_dup_allowed plus checks that the duplicate
    itself is still open. The canonical's current state and resolution do not
    affect whether the duplicate relationship is valid.

    `live_state` fetches an issue's current upstream state ("open"/"closed", or
    None when GitHub is unreachable). It is consulted only after the static checks
    pass, and a fetched value overrides the store's snapshot. A failed fetch falls
    back to the store."""
    ok, reason = close_dup_allowed(issue, cluster, today)
    if not ok:
        return ok, reason
    own = (live_state(issue.number) if live_state else None) or issue.state
    if own is not None and own != "open":
        return False, f"issue #{issue.number} is already {own} upstream — no action needed"
    return True, "confirmed duplicate — ready to close"


def close_fixed_eligibility(issue: Issue, fixed_by_state: str | None,
                            issue_live_state: str | None = None) -> tuple[bool, str]:
    """Executor's pre-write gate for closing an issue as fixed by a merged PR: the
    referenced PR must be currently `merged` and the issue still open. Deterministic
    — a merged fixer stands on its own, with no cluster-curation dependency (the
    app only offers this on a confirmed-cluster card). Both the PR's `merged`
    state and `issue_live_state` are resolved live by the caller; a fetched issue
    state overrides the store's snapshot, falling back to it when the fetch returns
    None (fail-open — a transient read failure must not block the gate)."""
    if fixed_by_state != "merged":
        return False, f"fixing PR is {fixed_by_state or 'unresolved'}, not merged"
    own = issue_live_state or issue.state
    if own != "open":
        return False, f"issue is {own} upstream — no action needed"
    return True, "fixing PR merged, issue open — ready to close as fixed"


def issue_cluster_state(cluster: IssueCluster, issues: dict[int, Issue],
                        today: str | None = None) -> str:
    """Derived board chip: needs-curation | dup-ready | awaiting-repro | no-dups | done."""
    members = [issues[n] for n in cluster.members if n in issues]
    active = [i for i in members if i.state == "open"]
    if members and not active:
        return "done"
    if cluster.needs_review or not (cluster.curation or {}).get("confirmed"):
        return "needs-curation"
    pending_dups = [i for i in active if i.disposition == "close-dup"
                    and (i.resolution or {}).get("status") in (None, "pending")]
    if pending_dups:
        return "dup-ready"
    if any(i.disposition == "request-repro" for i in active):
        return "awaiting-repro"
    if len(active) <= 1:
        return "no-dups"
    return "done"


REPRODUCTION_OUTCOMES = ("reproduced", "not-reproduced", "unwritable", "wrong-symptom",
                         "not-a-defect")
_CONFIDENCES = ("high", "medium", "low")


def _rating(judge: dict | None, key: str, flag: str) -> tuple[bool, bool] | None:
    """(the judge's answer, whether it holds it above low confidence), or None
    when the rating is missing or malformed."""
    part = (judge or {}).get(key)
    if not isinstance(part, dict) or not isinstance(part.get(flag), bool):
        return None
    if part.get("confidence") not in _CONFIDENCES:
        return None
    return part[flag], part["confidence"] != "low"


def reproduction_outcome(red: dict, judge: dict | None, *, gave_up: bool,
                         invalid: str | None) -> str | None:
    """What a reproduction attempt amounts to, from the host's two red exits and
    the judge's ratings. None is a machine fault: a sandbox exit that is not a
    test verdict, or a judge that gave no usable rating. The judge rates; this
    decides — a rating held with low confidence reads as a no."""
    if gave_up or invalid:
        return "unwritable"
    first, confirm = red.get("exit"), red.get("exit_confirm")
    if first == gates.SENTINEL_PASS or confirm == gates.SENTINEL_PASS:
        return "not-reproduced"
    if first != gates.SENTINEL_TEST_FAIL or confirm != gates.SENTINEL_TEST_FAIL:
        return None
    symptom = _rating(judge, "symptom_match", "matches")
    defect = _rating(judge, "defect", "is_defect")
    if symptom is None or defect is None:
        return None
    if symptom != (True, True):
        return "wrong-symptom"
    if defect != (True, True):
        return "not-a-defect"
    return "reproduced"


REVIEW_LENSES = ("root-cause", "scope-safety")
FIX_PATCH_MAX_CHARS = 200_000

# Diff lines a fix may not carry, with what each one is.
_REFUSED_DIFF_LINES = (
    ("Binary files ", "a binary file"), ("GIT binary patch", "a binary file"),
    ("new file mode 120000", "a symlink"), ("new file mode 160000", "a submodule"),
    ("old mode ", "a file-mode change"), ("new file mode 100755", "an executable file"),
)


def changed_line_count(patch: str) -> int:
    return sum(1 for line in patch.splitlines()
              if (line.startswith("+") and not line.startswith("+++"))
              or (line.startswith("-") and not line.startswith("---")))


def fix_patch_regate(patch: str, *, changes: list[author_fix.Change],
                     max_lines: int) -> tuple[bool, str]:
    """Whether an agent's finished fix may go on to proof: (ok, reason). The
    patch is held to what the agent reported and to the paths, size, and kinds
    of change an issue-driven fix may make. Fail-closed."""
    if not patch.startswith("diff "):
        return False, "the fix is empty or not a diff"
    if len(patch) > FIX_PATCH_MAX_CHARS:
        return False, f"the fix is over {FIX_PATCH_MAX_CHARS} characters"
    for line in patch.splitlines():
        for marker, what in _REFUSED_DIFF_LINES:
            if line.startswith(marker):
                return False, f"the fix carries {what}"
    paths = diffpaths.changed_paths(patch)
    if not paths:
        return False, "the fix names no path"
    try:
        author_fix.assert_disclosed(changes, paths)
    except ValueError as e:
        return False, str(e)
    tests = [p for p in paths if diffpaths.is_test_path(p)]
    if tests:
        return False, f"the fix touches test files: {', '.join(tests)}"
    if gates.deps_touched(paths):
        return False, "the fix changes a dependency manifest"
    withheld = gates.fix_withheld_paths(paths)
    if withheld:
        return False, f"the fix touches withheld paths: {', '.join(withheld)}"
    tier = risktier.pr_tier(paths)
    if tier is None or tier == 0:
        return False, "the fix touches a tier-0 path"
    scan = threats.scan_diff(patch)
    if scan["verdict"] == "malicious":
        return False, f"the fix matches a threat signature: {', '.join(scan['signatures'])}"
    count = changed_line_count(patch)
    if count > max_lines:
        return False, f"the fix changes {count} lines (limit {max_lines})"
    return True, "clean"


def _twice(legs: dict | None, want: int) -> bool:
    return bool(legs) and legs.get("exit") == want and legs.get("exit_confirm") == want


def fix_proof_bar(result: dict) -> tuple[str | None, str]:
    """(None, …) when a fix is proven and reviewed, else the ending it falls
    short at — "fix-unproven" for the host's proof, "fix-rejected" for a
    reviewer — with the reason. The proof is the reproduction red on the base
    and green with the fix, the preservation tests green with the fix, the
    compile lane, and the related tests. Only an explicit `safe` from every lens
    in REVIEW_LENSES passes."""
    proof = result.get("proof") or {}
    if not _twice(proof.get("red"), gates.SENTINEL_TEST_FAIL):
        return "fix-unproven", "the reproduction is not red twice on the base"
    if not _twice(proof.get("green"), gates.SENTINEL_PASS):
        return "fix-unproven", "the reproduction is not green twice with the fix applied"
    if not _twice(proof.get("preserve"), gates.SENTINEL_PASS):
        return "fix-unproven", ("the preservation tests do not pass twice with the fix "
                                "applied: it changes behavior they pin")
    compiled = proof.get("compile")
    if compiled is not None:
        not_run = compiled.get("refused") or compiled.get("error")
        if not_run or compiled.get("exit") != gates.SENTINEL_PASS:
            return "fix-unproven", ("the compile lane did not pass: "
                                    + str(not_run or compiled.get("error_excerpt")
                                          or f"exit {compiled.get('exit')}"))
    related = proof.get("related_tests")
    if related and not related.get("base_fails"):
        block = gates.related_tests_block(related, "the fix")
        if block:
            return "fix-unproven", block
    reviews = {r.get("lens"): r for r in result.get("reviews") or []}
    for lens in REVIEW_LENSES:
        review = reviews.get(lens)
        if review is None:
            return "fix-rejected", f"no {lens} review"
        if review.get("verdict") != "safe" or review.get("failed"):
            return "fix-rejected", f"{lens}: {review.get('reason') or 'not safe'}"
    return None, "proven on the base and safe under both lenses"
