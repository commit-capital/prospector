"""GitHub's on-PR bot feed: every review, review thread, issue comment and head
check run, fetched for a batch of PRs in one GraphQL call each and handed to
`pipeline.reviewers` to parse per bot."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pipeline import ci_signal, settings
from pipeline.gh import gh_graphql

_log = logging.getLogger(__name__)


@dataclass
class PrFeed:
    """One PR's raw bot-relevant activity. `conversation` is False when only the
    head's check runs were fetched (the PR's conversation is unchanged since the
    stored entry) — adapters then keep the stored conversation fields."""
    pr: int
    head_sha: str | None
    updated_at: str | None
    reviews: list[dict] = field(default_factory=list)     # {id, login, state, commit, body, at, url}
    threads: list[dict] = field(default_factory=list)     # {id, login, path, line, body, commit, original_commit, resolved, outdated, at, url}
    comments: list[dict] = field(default_factory=list)    # {id, login, body, at, updated_at, url}
    check_runs: list[dict] = field(default_factory=list)  # {app, name, status, conclusion, title, summary, url}
    statuses: list[dict] = field(default_factory=list)    # {context, state}
    conversation: bool = True


CHUNK_SIZE = 10

_FIELDS = (
    "number headRefOid updatedAt "
    "reviews(last: 40) { nodes { databaseId author { login __typename } state commit { oid } "
    "body submittedAt url } } "
    "reviewThreads(last: 100) { nodes { isResolved isOutdated comments(first: 1) { nodes { "
    "databaseId author { login } body path line originalLine commit { oid } "
    "originalCommit { oid } createdAt updatedAt url } } } } "
    "comments(last: 40) { nodes { databaseId author { login __typename } body createdAt "
    "updatedAt url } } "
    "commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { "
    "__typename ... on CheckRun { name status conclusion title summary detailsUrl url "
    "checkSuite { app { slug } } } ... on StatusContext { context state } } } } } } }")


def feed_query(numbers: list[int]) -> str:
    aliases = " ".join(f"p{i}: pullRequest(number: {int(n)}) {{ {_FIELDS} }}"
                       for i, n in enumerate(numbers))
    return (f'query {{ repository(owner: "{settings.repo_owner()}", '
            f'name: "{settings.repo_name()}") {{ {aliases} }} }}')


def _login(node: dict | None) -> str | None:
    return ((node or {}).get("author") or {}).get("login")


def feed_from_node(n: int, node: dict) -> PrFeed:
    """One PR's GraphQL node → its feed."""
    reviews = [{"id": r.get("databaseId"), "login": _login(r), "state": r.get("state"),
                "commit": (r.get("commit") or {}).get("oid"), "body": r.get("body"),
                "at": r.get("submittedAt"), "url": r.get("url")}
               for r in ((node.get("reviews") or {}).get("nodes") or []) if isinstance(r, dict)]
    threads: list[dict] = []
    for t in ((node.get("reviewThreads") or {}).get("nodes") or []):
        if not isinstance(t, dict):
            continue
        firsts = ((t.get("comments") or {}).get("nodes") or [None])
        first = firsts[0] if firsts else None
        if not isinstance(first, dict):
            continue
        threads.append({"id": first.get("databaseId"), "login": _login(first),
                        "path": first.get("path"),
                        "line": first.get("line") or first.get("originalLine"),
                        "body": first.get("body"),
                        "commit": (first.get("commit") or {}).get("oid"),
                        "original_commit": (first.get("originalCommit") or {}).get("oid"),
                        "resolved": bool(t.get("isResolved")), "outdated": bool(t.get("isOutdated")),
                        "at": first.get("createdAt"), "url": first.get("url")})
    comments = [{"id": c.get("databaseId"), "login": _login(c), "body": c.get("body"),
                 "at": c.get("createdAt"), "updated_at": c.get("updatedAt"), "url": c.get("url")}
                for c in ((node.get("comments") or {}).get("nodes") or []) if isinstance(c, dict)]
    commits = ((node.get("commits") or {}).get("nodes")) or [{}]
    rollup = ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") or {}
    runs, statuses = ci_signal.from_graphql_contexts((rollup.get("contexts") or {}).get("nodes") or [])
    return PrFeed(pr=int(n), head_sha=node.get("headRefOid"), updated_at=node.get("updatedAt"),
                  reviews=reviews, threads=threads, comments=comments, check_runs=runs,
                  statuses=statuses, conversation=True)


def fetch_feeds(numbers: list[int]) -> dict[int, PrFeed]:
    """Feeds for `numbers`, keyed by PR. A PR missing from the result failed to
    fetch (transient) and keeps its stored entry."""
    out: dict[int, PrFeed] = {}
    for i in range(0, len(numbers), CHUNK_SIZE):
        chunk = [int(n) for n in numbers[i:i + CHUNK_SIZE]]
        payload = gh_graphql(feed_query(chunk), timeout=120)
        if payload is None:
            _log.warning("review feed fetch failed for PRs %s-%s", chunk[0], chunk[-1])
            continue
        repo = (payload.get("data") or {}).get("repository") or {}
        for j, n in enumerate(chunk):
            node = repo.get(f"p{j}")
            if isinstance(node, dict):
                out[n] = feed_from_node(n, node)
    return out
