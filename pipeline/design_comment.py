"""The PR comment that shows an author the design findings holding their merge.

Only the findings the design bar fails on are posted (`reviewers.design_standing`:
open, blocking or major, and confirmed by the refuting verifier). The comment
ends in a marker naming the reviewed head and the findings' fingerprint: a head
gets one comment however often it is re-reviewed, one set of findings is posted
once whatever head it was found at, and a new head with different findings gets
a new comment, which notifies the author where an edit would not.
`TRIAGE_DESIGN_COMMENT` sets whose PRs it is posted on.
"""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from pipeline import design_review, gates, review_policy, reviewers, settings

if TYPE_CHECKING:
    from pipeline.model import Pr

_TAG = re.compile(r"<(?=[/!A-Za-z])")
_MENTION = re.compile(r"[@#](?=[A-Za-z0-9])")
_LINK = re.compile(r"[\[\]]")


def findings(pr: Pr) -> list[dict]:
    """The findings to post on `pr`: the design bar's failing ones, or none
    when the bar does not fail at the PR's head, the head is no longer
    design-reviewable, or its security review is RED."""
    if (not design_review.reviewable(pr) or pr.security_verdict == "RED"
            or review_policy.bar(pr, reviewers.DESIGN).status != reviewers.FAIL):
        return []
    return reviewers.design_standing(pr.review_entry(reviewers.DESIGN.id))


def fingerprint(found: list[dict]) -> str:
    key = "\n".join(sorted(f"{f.get('path')}\t{f.get('line')}\t{f.get('category')}\t{f.get('title')}"
                           for f in found))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def head_marker(head_sha: str) -> str:
    return f"prospector:design-findings head={head_sha}"


def findings_marker(fp: str) -> str:
    return f"prospector:design-findings fp={fp}"


def posted(pr: Pr) -> dict | None:
    """The comment recorded for the findings standing on `pr` now, or None."""
    comment = ((pr.review_entry(reviewers.DESIGN.id) or {}).get("extra") or {}).get("comment")
    if not isinstance(comment, dict):
        return None
    found = findings(pr)
    return comment if found and comment.get("fingerprint") == fingerprint(found) else None


def audience_refusal(pr: Pr) -> str | None:
    """Why `pr` is outside the audience `TRIAGE_DESIGN_COMMENT` names, or None."""
    audience = settings.design_comment_audience()
    if audience == "off":
        return "posting design findings is off (TRIAGE_DESIGN_COMMENT)"
    if audience == "maintainers" and not gates.priority_author(pr.author, pr.author_association):
        return "posting design findings is limited to maintainers' PRs"
    return None


def _inert(text: str) -> str:
    """`text` with no HTML tag or comment, markdown link or image, @-mention or
    #-reference left live."""
    text = _LINK.sub(lambda m: "\\" + m.group(), _TAG.sub("&lt;", text))
    return _MENTION.sub(lambda m: m.group() + "\u200b", text)


def body(found: list[dict], head_sha: str) -> str:
    count = f"{len(found)} design finding" + ("s" if len(found) != 1 else "")
    parts = [f"**Design review: {count} to address before this PR can merge**", "",
             f"An automated design review of `{head_sha[:7]}` found the problems below, each "
             "confirmed by a second reviewer. This PR is waiting on them: once a push "
             "addresses them, the next review of the new head clears the hold.", ""]
    for i, f in enumerate(found, 1):
        where = f"{f.get('path')}:{f['line']}" if f.get("line") is not None else str(f.get("path"))
        parts += [f"**{i}. {_inert(str(f.get('title') or ''))}** "
                  f"({f.get('severity')}, {f.get('category')})", "",
                  "<details><summary>Details</summary>", "",
                  f"`{_inert(where)}`", "", _inert(str(f.get("body") or "")), "",
                  "</details>", ""]
    parts.append(f"<!-- {head_marker(head_sha)} -->")
    parts.append(f"<!-- {findings_marker(fingerprint(found))} -->")
    return "\n".join(parts)
