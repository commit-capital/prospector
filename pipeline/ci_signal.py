"""CI verdict from GitHub's check runs and commit statuses.

Runs owned by a registry reviewer (pipeline.reviewers) are left out: a
reviewer's own check reads under the reviewer's name, so CI reflects the
repository's own workflows."""
from __future__ import annotations

from collections.abc import Sequence

from pipeline import reviewers

FAIL_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"})
OK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

CONTEXT_FIELDS = (
    "pageInfo { hasNextPage endCursor } "
    "nodes { __typename ... on CheckRun { name status conclusion title summary detailsUrl url "
    "checkSuite { app { slug } } } ... on StatusContext { context state } }")

# GraphQL pullRequest fields `from_graphql_pr` reads: the head's checks.
PR_FIELDS = (
    "baseRefName commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { "
    + CONTEXT_FIELDS + " } } } } }")

# GraphQL Ref fields `required_contexts` reads: the checks a branch's rulesets
# and branch protection require.
REF_FIELDS = (
    "branchProtectionRule { requiredStatusCheckContexts } "
    "rules(first: 100) { pageInfo { hasNextPage } nodes { parameters { ... on RequiredStatusChecksParameters { "
    "requiredStatusChecks { context } } } } }")


def unreported(check_runs: list[dict], statuses: list[dict],
               required: Sequence[str]) -> list[str]:
    """The required contexts no check run or status at the head carries."""
    seen = {r.get("name") for r in check_runs} | {s.get("context") for s in statuses}
    return [c for c in required if c not in seen]


def verdict(check_runs: list[dict], statuses: list[dict], *,
            required: Sequence[str] | None = (),
            exclude_apps: frozenset[str] | None = None) -> str | None:
    """Failure outranks pending. Unread requirements are unknown; missing
    required contexts are unreported. Skipped/neutral runs pass. None when
    nothing counts and no checks are required."""
    excluded = reviewers.app_slugs() if exclude_apps is None else exclude_apps
    saw_any = saw_fail = saw_pending = False
    for run in check_runs:
        if run.get("app") in excluded:
            continue
        saw_any = True
        if run.get("status") != "completed":
            saw_pending = True
        elif run.get("conclusion") in FAIL_CONCLUSIONS:
            saw_fail = True
        elif run.get("conclusion") not in OK_CONCLUSIONS:
            saw_pending = True
    for st in statuses:
        saw_any = True
        if st.get("state") in ("failure", "error"):
            saw_fail = True
        elif st.get("state") == "pending":
            saw_pending = True
    if saw_fail or saw_pending:
        return "failing" if saw_fail else "pending"
    if required is None:
        return "unknown"
    if unreported(check_runs, statuses, required):
        return "unreported"
    return "passing" if saw_any else None


def from_rest_check_runs(check_runs: list[dict]) -> list[dict]:
    """REST `check_runs` items → `{app, name, status, conclusion, title, summary, url}`."""
    out: list[dict] = []
    for r in check_runs:
        output = r.get("output") or {}
        out.append({"app": (r.get("app") or {}).get("slug"), "name": r.get("name"),
                    "status": r.get("status"), "conclusion": r.get("conclusion"),
                    "title": output.get("title"), "summary": output.get("summary"),
                    "url": r.get("html_url")})
    return out


def from_graphql_contexts(nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    """statusCheckRollup.contexts nodes → (check runs, statuses), lower-cased."""
    runs: list[dict] = []
    statuses: list[dict] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("__typename") == "CheckRun":
            runs.append({"app": ((node.get("checkSuite") or {}).get("app") or {}).get("slug"),
                         "name": node.get("name"),
                         "status": (node.get("status") or "").lower() or None,
                         "conclusion": (node.get("conclusion") or "").lower() or None,
                         "title": node.get("title"), "summary": node.get("summary"),
                         "url": node.get("url") or node.get("detailsUrl")})
        elif node.get("__typename") == "StatusContext":
            statuses.append({"context": node.get("context"),
                             "state": (node.get("state") or "").lower() or None})
    return runs, statuses


def check_contexts(node: dict) -> dict | None:
    commits = ((node.get("commits") or {}).get("nodes")) or [{}]
    rollup = ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") or {}
    return rollup.get("contexts")


def from_graphql_pr(node: dict) -> tuple[list[dict], list[dict]]:
    """A pullRequest node carrying `PR_FIELDS` → (check runs, statuses)."""
    return from_graphql_contexts((check_contexts(node) or {}).get("nodes") or [])


def required_contexts(ref: dict) -> list[str]:
    """A Ref node carrying `REF_FIELDS` → the contexts the branch requires."""
    required = list((ref.get("branchProtectionRule") or {}).get("requiredStatusCheckContexts") or [])
    for rule in (ref.get("rules") or {}).get("nodes") or []:
        for check in ((rule or {}).get("parameters") or {}).get("requiredStatusChecks") or []:
            required.append(check.get("context"))
    return list(dict.fromkeys(c for c in required if c))
