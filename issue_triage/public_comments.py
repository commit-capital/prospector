"""The comments the public loop (`issue_triage.public_loop`) posts on an issue
and on the pull request it opened: where the pull request stands, and that the
pipeline left the issue to a maintainer.

The host writes every sentence; an agent's words (a summary, a root cause, a
reviewer's reason) appear only as clipped, inert plain text: no HTML comment,
no closing keyword, and no issue reference GitHub would link back from. Each
comment ends with a marker naming the issue and the key it was posted under,
and `problems` gates the rendering before the bot posts it.
"""
from __future__ import annotations

import re

from issue_triage import dispute_question, fix_pr_body

MARKER = "<!-- prospector:issue-fix-public v1 issue={issue} key={key} -->"
FRAGMENT_MAX = 600
REASON_MAX = 300
FOOTER = ("_Written by Prospector's automated fix pipeline. Its analysis may be wrong; "
          "a maintainer has the final say._")
_CLOSING_RE = re.compile(r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)(\s+)#(\d+)")
# An issue or pull request reference (#12, owner/repo#12, GH-12) GitHub links.
_REF_RE = re.compile(r"(?i)(#|\bGH-)(?=\d)")


def marker(issue: int, key: str) -> str:
    return MARKER.format(issue=issue, key=key)


def quiet(text: object, limit: int = FRAGMENT_MAX) -> str:
    """An agent's text as one inert line: HTML comment markers removed, closing
    keywords and issue references defused, and its closing period dropped (the
    template supplies one)."""
    one = str(text or "").replace("<!--", "").replace("-->", "")
    one = _CLOSING_RE.sub(r"\1\2issue \3", fix_pr_body.inert(one, limit))
    return _REF_RE.sub("\\g<1>\u200b", one).rstrip(". ")


def _finish(issue: int, key: str, lines: list[str]) -> str:
    return "\n".join([*lines, "", FOOTER, "", marker(issue, key)]) + "\n"


def capped(issue: int, key: str, attempts: int) -> str:
    """The comment once replies have started as many attempts as they may."""
    return _finish(issue, key, [
        f"Prospector's automated fix pipeline has made {int(attempts)} more attempts on this "
        "issue after replies here, and leaves it to a maintainer from now on. Replies no "
        "longer start another attempt."])


def opened(issue: int, key: str, pr: int, summary: object) -> str:
    """The comment on issue `issue` once pull request `pr` carries its fix."""
    lines = [f"Prospector's automated fix pipeline opened #{int(pr)} with a fix for this "
             "issue. It keeps revising that pull request until CI and code review pass, "
             "then labels it `ready for review`."]
    what = quiet(summary)
    if what:
        lines += ["", f"The change: {what}."]
    return _finish(issue, key, lines)


def recommended(issue: int, key: str, pr: int) -> str:
    """The comment on pull request `pr` once an operator chooses it as issue
    `issue`'s fix (`issue_triage.fix_choice`)."""
    return _finish(issue, key, [
        f"Prospector recommends this pull request as the fix for #{int(issue)}. It goes "
        "ahead in Prospector's verification, security and design review queues."])


def ready(issue: int, key: str) -> str:
    """The comment on the pull request once CI and its reviewers pass."""
    return _finish(issue, key, [
        "CI and code review pass on this pull request. It is ready for a maintainer's review."])


def handed_back(issue: int, key: str, reason: object) -> str:
    """The comment on the pull request when the follow-up stops revising it."""
    why = quiet(reason, REASON_MAX)
    return _finish(issue, key, [
        "Prospector's automated fix pipeline has stopped revising this pull request"
        + (f": {why}." if why else "."),
        "", "It needs a maintainer to take it from here."])


def _ask(text: object, limit: int = REASON_MAX) -> str:
    """An agent's request to the author as one inert sentence."""
    one = quiet(text, limit)
    return one if one.endswith("?") else f"{one}."


def triage_question(issue: int, key: str, question: object, readings: list,
                    gaps: list[dict] | None = None) -> str:
    """The comment on issue `issue` when its report falls short of the bar:
    each of the triage's `gaps` lettered, with the `readings` under the
    unsettled one, or, for a triage that recorded none, its one question."""
    shown = [quiet(r, REASON_MAX) for r in readings[:5] if quiet(r, REASON_MAX)]
    gaps = [g for g in gaps or [] if quiet(g.get("ask"))]
    if gaps:
        lines = ["Prospector's automated fix pipeline read this report against the current code "
                 "and needs the following from you before it tries a fix:", ""]
        for i, g in enumerate(gaps[:5]):
            lines.append(f"- **{chr(65 + i)}.** {_ask(g['ask'], FRAGMENT_MAX)}")
            if g.get("kind") == "unsettled":
                lines += [f"  - {r}." for r in shown]
        lines += ["", "Editing the report or replying here starts the fix again."]
        return _finish(issue, key, lines)
    lines = ["Prospector's automated fix pipeline read this report against the current code "
             "and needs one thing from you before it tries a fix:", "",
             f"> {quiet(question)}?"]
    if shown:
        lines += ["", "The readings it sees:",
                  *[f"- **{chr(65 + i)}.** {r}." for i, r in enumerate(shown)]]
    lines += ["", "A reply here starts the fix with your answer."]
    return _finish(issue, key, lines)


def problems(body: str) -> list[str]:
    """Why a rendered comment may not be posted; empty when it may."""
    return dispute_question.problems(body)
