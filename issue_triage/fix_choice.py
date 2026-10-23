"""The ONE policy for an operator's choice of another author's pull request as
an issue's fix, in place of the factory's own.

`choose` records the choice on the issue (`fix_choice`) and on the pull
request (`backing`), and cancels the issue's queued fix request. While the
choice stands (`chosen`), the issue's fix status reads `chosen`
(`fix_review.fix_status`), so no hunter, public-loop pass or reply starts or
proposes a fix of ours, and the pull request goes ahead in the PR queues
(`gates.queue_rank`). The choice lapses on read once the issue's links record
the pull request closed without merging. `undo` removes both sections. The
bot's comment on the pull request is the caller's to post (`comment`).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from issue_triage import fix_review, public_comments
from pipeline import storekit

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from issue_triage.issue_store import IssueStore
    from pipeline.store import Store


def chosen(issue: Issue) -> int | None:
    """The pull request chosen as `issue`'s fix while the choice stands."""
    choice = issue.fix_choice
    if not choice:
        return None
    pr = int(choice["pr"])
    if any(int(link.get("pr") or 0) == pr and link.get("state") == "closed"
           for link in issue.github_links):
        return None
    return pr


def choose(issues: IssueStore, prs: Store, n: int, pr: int, *, by: str,
           reason: str | None) -> tuple[bool, str]:
    """Choose open pull request `pr` as issue `n`'s fix. (ok, why)."""
    issue = issues.load_issue(n)
    if issue is None or issue.state != "open":
        return False, f"#{n} is not an open issue"
    rec = prs.load_pr(pr)
    if rec is None or rec.state != "open":
        return False, f"#{pr} is not an open pull request in the store"
    if (issue.fix_request or {}).get("status") == "running":
        return False, "a fix request is running; wait for it to finish"
    ours = fix_review.open_pr(issue)
    if ours is not None:
        return False, f"our fix is open as #{ours}; close it first"
    at = storekit.now()
    words = (reason or "").strip() or None
    fix_review.cancel(issues, n, by=by)
    prs.set_pr_section(pr, "backing", {"issue": n, "by": by, "at": at, "reason": words})
    issues.set_issue_section(n, "fix_choice", {"pr": pr, "by": by, "at": at, "reason": words})
    issues.edit_issue(n).append_fix_thread({
        "at": at, "by": by, "kind": "comment",
        "text": f"Going with #{pr}" + (f": {words}" if words else "")})
    return True, f"going with #{pr}"


def undo(issues: IssueStore, prs: Store, n: int, *, by: str) -> tuple[bool, str]:
    """Withdraw issue `n`'s choice, so the factory may fix it again. The bot's
    comment stays on the pull request."""
    issue = issues.load_issue(n)
    choice = issue.fix_choice if issue else None
    if not choice:
        return False, f"#{n} has no chosen pull request"
    pr = int(choice["pr"])
    rec = prs.load_pr(pr)
    if rec is not None and (rec.section("backing") or {}).get("issue") == n:
        prs.set_pr_section(pr, "backing", None)
    issues.set_issue_section(n, "fix_choice", None)
    issues.edit_issue(n).append_fix_thread({"at": storekit.now(), "by": by, "kind": "comment",
                                            "text": f"No longer going with #{pr}"})
    return True, f"no longer going with #{pr}"


def comment(n: int, pr: int) -> tuple[str, str]:
    """The bot's comment on pull request `pr` for issue `n`, and its marker."""
    key = f"choose-{pr}"
    return public_comments.recommended(n, key, pr), public_comments.marker(n, key)
