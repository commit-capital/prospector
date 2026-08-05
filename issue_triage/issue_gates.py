"""The ONE issue close-as-dup gate policy + derived cluster state.

close_dup_allowed is the pipeline's static auto-recommend (drives the dup
worklist); close_dup_eligibility adds live upstream checks that the duplicate and
is still open, and is the app/executor's pre-write gate — the issue-side analog of
gates.py's merge_allowed vs merge_eligibility.
issue_cluster_state is the derived board chip, computed on read, never stored.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from issue_triage.issue_freshness import is_current

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue, IssueCluster

# Issue disposition precedence: the evidence-backed close proposals outrank the
# generic needs-human flag (every close is still human-approved at execution, so
# a stronger proposal surfacing costs nothing), and close-dup outranks
# close-fixed. Lower index = higher precedence. The ONE place this order is
# defined; the PR-side order is gates.DISPOSITION_PRECEDENCE and differs.
DISPOSITION_PRECEDENCE: tuple[str, ...] = ("close-dup", "close-fixed", "needs-human")
_DISPOSITION_RANK = {d: i for i, d in enumerate(DISPOSITION_PRECEDENCE)}


def disposition_outranks(new: str, current: str | None) -> bool:
    """True when issue disposition `new` should replace `current` under
    DISPOSITION_PRECEDENCE. A disposition absent from the table (a keep-open
    state such as request-repro or link-pr) ranks below every listed one, and a
    None current is always replaced."""
    if current is None:
        return True
    low = len(DISPOSITION_PRECEDENCE)
    return _DISPOSITION_RANK.get(new, low) <= _DISPOSITION_RANK.get(current, low)


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
        return False, "cluster duplicates not human-confirmed (curation pending)"
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
