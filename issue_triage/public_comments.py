"""The comments the public loop (`issue_triage.public_loop`) posts on an issue
and on the pull request it opened: what a fix attempt concluded, and where the
pull request stands.

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
# The existing tests a `fix-pinned` conclusion lists.
PINNED_MAX = 5
FOOTER = ("_Written by Prospector's automated fix pipeline. Its analysis may be wrong; "
          "a maintainer has the final say._")
_CLOSING_RE = re.compile(r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)(\s+)#(\d+)")
# An issue or pull request reference (#12, owner/repo#12, GH-12) GitHub links.
_REF_RE = re.compile(r"(?i)(#|\bGH-)(?=\d)")

# The conclusion comment each ending without a fix gets.
NOT_REPRODUCED = frozenset({"not-reproduced", "wrong-symptom", "unwritable"})

# What stopped a fix, by ending, in a maintainer's words.
_SHORT_OF = {
    "no-fix": "none of the independent attempts produced a fix",
    "fix-untrusted": "none of the fixes could be verified against the reproduction",
    "fix-unproven": "the fix was not proven by every check",
    "fix-rejected": "a reviewer judged the change unsafe",
    "fix-disputed": "the independent attempts disagreed on the correct behavior, and no "
                    "single question could settle it",
    "declined": "the attempt stopped before it could finish",
}


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


def kind_for(ending: str) -> str:
    """The conclusion comment an ending without a fix gets: `not-reproduced`,
    `not-a-defect`, or `no-fix`."""
    if ending in NOT_REPRODUCED:
        return "not-reproduced"
    if ending == "not-a-defect":
        return "not-a-defect"
    return "no-fix"


def conclusion(issue: int, key: str, run: dict) -> str:
    """The comment for issue `issue`'s attempt `run` (a `fix_run` section) that
    ended without a fix."""
    ending = str(run.get("ending") or "")
    detail = quiet(run.get("detail"), REASON_MAX)
    root = quiet(run.get("root_cause"))
    kind = kind_for(ending)
    if kind == "not-reproduced":
        lines = ["Prospector's automated fix pipeline tried to fix this issue, but it could "
                 "not reproduce the problem."]
        if detail:
            lines += ["", f"What happened: {detail}."]
        lines += ["", "Exact steps to reproduce, the version in use, or the full error "
                      "would give it what it is missing: a reply here with them starts "
                      "another attempt."]
        return _finish(issue, key, lines)
    if kind == "not-a-defect":
        lines = ["Prospector's automated fix pipeline looked into this issue and read the "
                 "reported behavior as intended rather than a defect."]
        if root or detail:
            lines += ["", f"Its reasoning: {root or detail}."]
        lines += ["", "If the behavior should be different, a reply here describing the "
                      "expected behavior starts another attempt."]
        return _finish(issue, key, lines)
    if ending == "fix-pinned":
        tests = ((run.get("agreement") or {}).get("pinned") or {}).get("tests") or []
        lines = ["Prospector's automated fix pipeline reproduced this issue, and its "
                 "independent attempts agreed on a fix, but every such fix fails existing "
                 "tests that pass today:", ""]
        lines += [f"- {quiet(t, REASON_MAX)}" for t in tests[:PINNED_MAX]]
        lines += ["", "Either those tests pin the behavior this issue asks to change, or the "
                      "fix breaks something they guard. The pipeline never changes a "
                      "repository's own tests, so updating them is a maintainer's call: if the "
                      "requested behavior is wanted, a reply here once that change has landed "
                      "starts another attempt."]
        return _finish(issue, key, lines)
    lines = ["Prospector's automated fix pipeline tried to fix this issue, but it could not "
             "land a fix that passed every check."]
    reproduced = any(c.get("reproduces") for c in run.get("candidates") or [])
    if reproduced:
        lines += ["", "It did reproduce the problem."]
    if root:
        lines += ["", f"Its reading of the cause: {root}."]
    short = _SHORT_OF.get(ending, "the attempt did not finish with a fix")
    lines += ["", f"Where it fell short: {short}" + (f" ({detail})." if detail else "."),
              "", "A reply here with more detail, the intended behavior, or a pointer to the "
                  "cause starts another attempt."]
    return _finish(issue, key, lines)


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


def problems(body: str) -> list[str]:
    """Why a rendered comment may not be posted; empty when it may."""
    return dispute_question.problems(body)
