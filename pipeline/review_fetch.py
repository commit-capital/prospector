"""GitHub's on-PR bot feed: every review, review thread, issue comment and head
check run, fetched for a batch of PRs in one GraphQL call each and handed to
`pipeline.reviewers` to parse per bot."""
from __future__ import annotations

from dataclasses import dataclass, field


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
