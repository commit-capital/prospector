"""CI verdict from GitHub's check runs and commit statuses.

Runs owned by a registry reviewer (pipeline.reviewers) are left out: a
reviewer's own check reads under the reviewer's name, so CI reflects the
repository's own workflows."""
from __future__ import annotations

from pipeline import reviewers

FAIL_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"})
OK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


def verdict(check_runs: list[dict], statuses: list[dict], *,
            exclude_apps: frozenset[str] | None = None) -> str | None:
    """'passing' | 'failing' | 'pending', or None when nothing counts. Failure
    outranks pending outranks passing; skipped/neutral do not fail a run."""
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
    if not saw_any:
        return None
    return "failing" if saw_fail else "pending" if saw_pending else "passing"


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
