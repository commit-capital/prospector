"""Read-only fetch of issues from the configured repo.

The bulk fetch pages the GraphQL issues connection (issues only — no PRs to
filter, no shared PR-interleave pagination budget), and `fetch_numbers` reads
named issues through the same GraphQL selection. Single-issue refetch uses the
REST issues endpoint. All capture the engagement signals the pain score needs
(reactions, comments, author) plus the issue's state, so closures can be written
back to the store. Only the GraphQL reads see GitHub's own closing references,
the body's edit time, and the content time (`content_updated_at`); the REST
refetch reports them unknown.
"""
from __future__ import annotations

import time

from issue_triage.config import repo, repo_name, repo_owner
from pipeline import gh
from pipeline import progress
from pipeline import settings
from pipeline import storekit

# How many of an issue's newest comments a GraphQL read carries, for the
# content time.
COMMENTS_READ = 20

# The fields every GraphQL issue read selects, named to normalize onto the same
# shape normalize_issue produces from REST, so ingest is agnostic to which
# transport fetched a row. The newest comments carry their authors' types and
# the timeline its newest title rename or reopen, for the content time.
_ISSUE_FIELDS = f"""
        number title body
        state stateReason
        createdAt updatedAt lastEditedAt
        author {{ login }}
        authorAssociation
        assignees(first:10) {{ nodes {{ login }} }}
        labels(first:50) {{ nodes {{ name }} }}
        comments(last:{COMMENTS_READ}) {{
          totalCount
          nodes {{ createdAt lastEditedAt author {{ login __typename }} }}
        }}
        reactions {{ totalCount }}
        reactionGroups {{ content reactors {{ totalCount }} }}
        closedByPullRequestsReferences(first:10, includeClosedPrs:true) {{
          nodes {{ number state isDraft }}
        }}
        timelineItems(last:1, itemTypes:[RENAMED_TITLE_EVENT, REOPENED_EVENT]) {{
          nodes {{
            ... on RenamedTitleEvent {{ createdAt }}
            ... on ReopenedEvent {{ createdAt }}
          }}
        }}
"""

# GraphQL issues connection: one page of 100 open issues + a cursor.
_ISSUES_QUERY = f"""
query($owner:String!, $name:String!, $cursor:String) {{
  repository(owner:$owner, name:$name) {{
    issues(states:OPEN, first:100, after:$cursor) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{{_ISSUE_FIELDS}      }}
    }}
  }}
}}
"""

# How many issues one `fetch_numbers` query reads.
_NUMBERS_PER_QUERY = 50


def is_pull_request(raw: dict) -> bool:
    return bool(raw.get("pull_request"))


def normalize_issue(raw: dict) -> dict:
    """Normalize a REST issues-endpoint payload (the single-issue refetch path).

    The payload carries neither the body's edit time, its comments' authors, nor
    GitHub's closing references, so the edit time, the content time and the
    closing references read None. The None closing references mark the row a
    partial view, and ingest keeps the stored value of every fact it leaves
    out."""
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
        "author_association": raw.get("author_association"),
        "assignees": [a["login"] for a in raw.get("assignees") or []],
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "last_edited_at": None,
        "content_updated_at": None,
        "state_reason": raw.get("state_reason"),
        "github_links": None,
    }


def _thumbs_up(groups: list[dict] | None) -> int:
    """Count of 👍 reactions, pulled from the GraphQL reactionGroups list (REST
    hands this back directly as reactions['+1'])."""
    for g in groups or []:
        if g.get("content") == "THUMBS_UP":
            return (g.get("reactors") or {}).get("totalCount", 0)
    return 0


def _closing_refs(node: dict) -> list[dict]:
    """The PRs GitHub reports as closing this issue, one `{pr, state, draft}` each,
    over the first ten the query asks for. States are lowercased to match the
    store's PR vocabulary."""
    refs = (node.get("closedByPullRequestsReferences") or {}).get("nodes") or []
    return [{"pr": ref["number"], "state": (ref.get("state") or "").lower(),
             "draft": bool(ref.get("isDraft"))} for ref in refs]


def _automated(author: dict | None) -> bool:
    """Whether a comment's author is a GitHub App or the configured bot. GraphQL
    types an App's account `Bot` with a bare login; the `[bot]` suffix names one
    where the type is absent. A comment whose author GitHub no longer reports
    (a deleted account) is not automated."""
    if not author:
        return False
    login = author.get("login") or ""
    bot = settings.bot_login().removesuffix("[bot]")
    return (author.get("__typename") == "Bot" or login.endswith("[bot]")
            or (bool(bot) and login.removesuffix("[bot]") == bot))


def _content_updated_at(node: dict) -> str | None:
    """When the issue last changed in a way its facts depend on: the latest of
    its creation, the body's last edit, its newest title rename or reopen, and
    the creation or last edit of each comment not written by automation, never
    later than updatedAt. Labels, assignments, milestones, reactions and
    automation's comments leave it where it is. When every comment read is
    automation's and the thread holds more than were read, the oldest read
    comment's creation stands for the unread ones, the latest any of them can
    have been posted."""
    comments = node.get("comments") or {}
    read = comments.get("nodes") or []
    people = [c for c in read if not _automated(c.get("author"))]
    times: list[str | None] = [node.get("createdAt"), node.get("lastEditedAt")]
    times += [e.get("createdAt") for e in (node.get("timelineItems") or {}).get("nodes") or []]
    for c in people:
        times += [c.get("createdAt"), c.get("lastEditedAt")]
    if read and not people and (comments.get("totalCount") or 0) > len(read):
        times.append(read[0].get("createdAt"))
    stamped = [(at, t) for t in times if t and (at := storekit.parse_ts(t)) is not None]
    if not stamped:
        return None
    latest_at, latest = max(stamped)
    updated = node.get("updatedAt")
    updated_at = storekit.parse_ts(updated)
    if updated_at is not None and latest_at > updated_at:
        return updated
    return latest


def normalize_gql(node: dict) -> dict:
    """Map a GraphQL issue node onto the exact dict normalize_issue produces from
    REST. State enums are lowercased to match REST ('OPEN'→'open')."""
    labels = (node.get("labels") or {}).get("nodes") or []
    assignees = (node.get("assignees") or {}).get("nodes") or []
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
        "author_association": node.get("authorAssociation"),
        "assignees": [a["login"] for a in assignees],
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "last_edited_at": node.get("lastEditedAt"),
        "content_updated_at": _content_updated_at(node),
        "state_reason": reason.lower() if reason else None,
        "github_links": _closing_refs(node),
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

    Where `progress` reports, prints a line per page fetched.
    """
    rows: list[dict] = []
    cursor: str | None = None
    report = progress.enabled()
    started = time.monotonic()
    for page_no in range(1, max_pages + 1):
        variables = {"owner": repo_owner(), "name": repo_name()}
        if cursor:
            variables["cursor"] = cursor
        conn = gh.gh_graphql_data(_ISSUES_QUERY, variables=variables,
                                  rate_limit_waits=gh.RATE_LIMIT_BACKOFF)["repository"]["issues"]
        rows += [normalize_gql(n) for n in conn["nodes"]]
        if report:
            progress.say(f"  page {page_no}: {len(rows):,} open issues so far · "
                         f"{progress.duration(time.monotonic() - started)}")
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


def fetch_numbers(numbers: list[int]) -> list[dict] | None:
    """Issues `numbers`, each read through the GraphQL selection the bulk fetch
    uses and normalized (read-only). An issue GitHub does not resolve (deleted,
    transferred, or a pull request) is left out. None when a query went
    unanswered."""
    rows: list[dict] = []
    variables = {"owner": repo_owner(), "name": repo_name()}
    for start in range(0, len(numbers), _NUMBERS_PER_QUERY):
        chunk = [int(n) for n in numbers[start:start + _NUMBERS_PER_QUERY]]
        reads = " ".join(f"i{n}: issue(number:{n}) {{{_ISSUE_FIELDS}}}" for n in chunk)
        query = ("query($owner:String!, $name:String!) { "
                 f"repository(owner:$owner, name:$name) {{ {reads} }} }}")
        data = (gh.gh_graphql(query, variables=variables) or {}).get("data")
        repository = data.get("repository") if isinstance(data, dict) else None
        if not isinstance(repository, dict):
            return None
        rows += [normalize_gql(node) for n in chunk
                 if isinstance(node := repository.get(f"i{n}"), dict)]
    return rows


def fetch_issue(n: int) -> dict | None:
    """Targeted fetch of one issue (read-only), normalized. Returns None when the
    fetch fails (deleted, transferred, or a transient API error) so the caller
    keeps its stored record untouched."""
    raw = gh.gh_json(f"repos/{repo()}/issues/{n}")
    return normalize_issue(raw) if raw is not None else None
