"""The ONE policy for stepping an issue's fix attempt aside when someone else's
pull request takes the issue up.

Until the attempt has a pull request of its own open, an open pull request whose
title or body names the issue (`related_prs.search`, the search the propose step
refuses on), by anyone but the bot and the push user, supersedes it. `check`
reads that search fresh and records one such pull request on the attempt as
`fix_run.superseded` (the oldest that claims to fix the issue, else the oldest),
or clears the mark when none is open; a search that
does not answer leaves the mark as it was. Once the attempt's own pull request
is open, the follow-up decides instead (`followup.decide` hands it back when an
older one names the issue).

Every reader of the attempt takes the mark as final: `fix_review.fix_status`
reads `superseded`, the public loop takes its label off and starts nothing, and
the question poll queues no answer. The runner checks before and after every
request, so a request on a superseded attempt ends `cancelled` and an operator's
request after the rival closed clears the mark and runs. `sweep` checks the
attempts waiting on their next step between requests, at most SWEEP_LIMIT
searches a pass.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from issue_triage import fix_review, related_prs
from pipeline import settings, storekit

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from issue_triage.issue_store import IssueStore

# The searches one sweep makes; GitHub's search API answers thirty a minute.
SWEEP_LIMIT = 20
# The statuses of an attempt waiting on a step the automation would take next.
WAITING = ("review", "question", "reporter", "failed")


class Mark(TypedDict):
    pr: int
    author: str | None
    title: str
    at: str


def _ours(login: str | None) -> bool:
    if not login:
        return False
    bot = settings.bot_login().removesuffix("[bot]")
    return login == settings.push_login() or (bool(bot) and login.removesuffix("[bot]") == bot)


def check(store: IssueStore, n: int) -> Mark | None:
    """The pull request that supersedes issue `n`'s attempt, with the attempt's
    mark brought in line with a fresh search. None when the attempt's own pull
    request is open, or when no rival is open; when the search does not answer,
    the mark the attempt already carries. An issue with no attempt yet gets its
    rival reported and nothing recorded."""
    issue = store.load_issue(n)
    if issue is None or fix_review.open_pr(issue) is not None:
        return None
    run = issue.fix_run or {}
    ours = (run.get("proposal") or {}).get("pr")
    found = related_prs.search(n, exclude={int(ours)} if ours else None)
    if found is None:
        return run.get("superseded")
    rivals = sorted((r for r in found if r["state"] == "open" and not _ours(r["author"])),
                    key=lambda r: (not r.get("closes"), r["number"]))
    mark: Mark | None = None
    if rivals:
        held = run.get("superseded") or {}
        first = rivals[0]
        kept = held.get("pr") == first["number"] and held.get("at")
        mark = {"pr": first["number"], "author": first["author"],
                "title": first["title"][:200], "at": str(kept) if kept else storekit.now()}
    if run and run.get("superseded") != mark:
        edit = store.edit_issue(n)
        updated = {k: v for k, v in (edit.fix_run or {}).items() if k != "superseded"}
        edit.record_fix_run({**updated, "superseded": mark} if mark else updated)
    return mark


def _waiting(issue: Issue) -> bool:
    status = fix_review.fix_status(issue)
    return (issue.state == "open" and status is not None and status[0] in WAITING
            and fix_review.open_pr(issue) is None)


def sweep(store: IssueStore, *, limit: int = SWEEP_LIMIT) -> list[int]:
    """Check every attempt waiting on its next step, newest issue first, at most
    `limit` of them. The issues it found superseded."""
    marked: list[int] = []
    issues = store.all_issues(omit_candidates=True)
    waiting = [n for n in sorted(issues, reverse=True) if _waiting(issues[n])]
    for n in waiting[:limit]:
        if check(store, n) is not None:
            marked.append(n)
    return marked
