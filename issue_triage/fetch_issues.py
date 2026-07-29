"""Read-only fetch of issues from the configured repo.

The bulk fetch pages the GraphQL issues connection (issues only — no PRs to
filter, no shared PR-interleave pagination budget). Single-issue refetch uses the
REST issues endpoint. Both capture the engagement signals the pain score needs
(reactions, comments, author) plus the issue's state, so closures can be written
back to the store.
"""
from __future__ import annotations

import subprocess

from issue_triage.config import REPO, REPO_NAME, REPO_OWNER, gh_graphql, gh_read

# GraphQL issues connection: one page of 100 open issues + a cursor. Fields are
# named to normalize onto the same shape normalize_issue produces from REST, so
# ingest is agnostic to which transport fetched a row.
_ISSUES_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    issues(states:OPEN, first:100, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body
        state stateReason
        createdAt updatedAt
        author { login }
        labels(first:50) { nodes { name } }
        comments { totalCount }
        reactions { totalCount }
        reactionGroups { content reactors { totalCount } }
      }
    }
  }
}
"""


def is_pull_request(raw: dict) -> bool:
    return bool(raw.get("pull_request"))


def normalize_issue(raw: dict) -> dict:
    """Normalize a REST issues-endpoint payload (the single-issue refetch path)."""
    reactions = raw.get("reactions") or {}
    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "body": raw.get("body") or "",
        "state": raw.get("state", "open"),
        "labels": [lbl["name"] for lbl in raw.get("labels", [])],
        "comments": raw.get("comments", 0),
        "reactions_total": reactions.get("total_count", 0),
        "thumbs_up": reactions.get("+1", 0),
        "author": (raw.get("user") or {}).get("login", ""),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "state_reason": raw.get("state_reason"),
    }


def _thumbs_up(groups: list[dict] | None) -> int:
    """Count of 👍 reactions, pulled from the GraphQL reactionGroups list (REST
    hands this back directly as reactions['+1'])."""
    for g in groups or []:
        if g.get("content") == "THUMBS_UP":
            return (g.get("reactors") or {}).get("totalCount", 0)
    return 0


def normalize_gql(node: dict) -> dict:
    """Map a GraphQL issue node onto the exact dict normalize_issue produces from
    REST. State enums are lowercased to match REST ('OPEN'→'open')."""
    labels = (node.get("labels") or {}).get("nodes") or []
    reason = node.get("stateReason")
    return {
        "number": node["number"],
        "title": node.get("title") or "",
        "body": node.get("body") or "",
        "state": (node.get("state") or "OPEN").lower(),
        "labels": [lbl["name"] for lbl in labels],
        "comments": (node.get("comments") or {}).get("totalCount", 0),
        "reactions_total": (node.get("reactions") or {}).get("totalCount", 0),
        "thumbs_up": _thumbs_up(node.get("reactionGroups")),
        "author": (node.get("author") or {}).get("login", ""),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "state_reason": reason.lower() if reason else None,
    }


def fetch_all(max_pages: int = 60, max_issues: int | None = None) -> list[dict]:
    """Page every open issue via the GraphQL issues connection (read-only).

    `max_issues` caps the result and stops paginating early (smoke runs).

    `max_pages` is a defensive backstop, not an expected stop: at 100 issues/page
    it allows 6,000 issues. Exhausting it while the connection still reports more
    pages means the corpus outgrew the backstop — this raises rather than return a
    truncated set, because a partial open-fetch makes the missing tail look closed
    to reconcile_closures. Smoke runs (max_issues) stop before the backstop, so it
    can't fire there.
    """
    rows: list[dict] = []
    cursor: str | None = None
    for _ in range(max_pages):
        variables = {"owner": REPO_OWNER, "name": REPO_NAME}
        if cursor:
            variables["cursor"] = cursor
        conn = gh_graphql(_ISSUES_QUERY, variables)["repository"]["issues"]
        rows += [normalize_gql(n) for n in conn["nodes"]]
        if max_issues is not None and len(rows) >= max_issues:
            return rows[:max_issues]
        page = conn["pageInfo"]
        if not page["hasNextPage"]:
            return rows
        cursor = page["endCursor"]
    raise RuntimeError(
        f"open-issues fetch hit the {max_pages}-page GraphQL backstop with more "
        f"pages remaining ({len(rows)} issues fetched). Raise fetch_all's "
        f"max_pages — refusing a truncated fetch, since reconcile_closures would "
        f"mark the un-fetched tail as closed.")


def fetch_issue(n: int) -> dict | None:
    """Targeted fetch of one issue (read-only), normalized. Returns None when the
    fetch fails (deleted, transferred, or a transient API error) so the caller
    keeps its stored record untouched."""
    try:
        return normalize_issue(gh_read(f"repos/{REPO}/issues/{n}"))
    except subprocess.CalledProcessError:
        return None
