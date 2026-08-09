"""Join issues to candidate fixing PRs.

Four signals, strongest evidence first: (a) explicit Fixes/Closes/Resolves #N
references parsed from PR bodies, matched whatever the PR's state — a merged
fixer is the durable "likely fixed" evidence; (b) any other #N in a PR body as a
lower-confidence body reference; (c) PR references parsed from the issue's own
text ("PR #626", a pull URL, "fixed by #627") — the reporter naming the PR that
addresses (or failed to address) their bug; (d) subsystem match against the same
taxonomy issues are tagged with, open PRs only. Surfaces orphan high-pain issues
(priority gaps) and which PRs map to top pain clusters (promote in the Wave plan).
"""
from __future__ import annotations

import re

REF = re.compile(
    r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*:?[ \t]*#(\d+)", re.I)
BODY_REF = re.compile(r"(?<![\w#])#(\d+)\b")

# A broad subsystem can tag-match hundreds of open PRs, none of them a real fix
# signal; cap how many "subsystem" candidates are kept per issue so the stored
# list stays small. Every direct ("explicit"/"body-ref"/"issue-ref") match is
# always kept.
SUBSYSTEM_CAP = 8

# PR references in issue text. A bare "#N" is usually another issue, so only
# PR-shaped forms count: "PR #N", a github pull URL, or a fix/close/resolve verb
# followed by by/in/via. Matches are validated against the PR corpus.
ISSUE_PR_REF = re.compile(
    r"\bPR\s*#(\d+)\b"
    r"|github\.com/[\w.-]+/[\w.-]+/pull/(\d+)\b"
    r"|\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+(?:by|in|via)\s+(?:PR\s*)?#(\d+)\b",
    re.I)


def parse_issue_refs(body: str | None) -> set[int]:
    return {int(m) for m in REF.findall(body or "")}


def parse_body_issue_refs(body: str | None) -> set[int]:
    """All issue-shaped ``#N`` references in a PR body.

    Callers validate these numbers against the issue corpus. Unlike
    :func:`parse_issue_refs`, this intentionally includes prose references such
    as ``Refs upstream issue #3846`` and is therefore lower-confidence evidence.
    """
    return {int(m) for m in BODY_REF.findall(body or "")}


def classify_issue_refs(body: str | None) -> dict[int, str]:
    """Issue number → strongest PR-body evidence (explicit or body-ref)."""
    explicit = parse_issue_refs(body)
    return {n: "explicit" if n in explicit else "body-ref"
            for n in parse_body_issue_refs(body)}


def parse_refs(prs: list[dict]) -> list[dict[int, str]]:
    """Each PR's classified references, positionally aligned with ``prs``."""
    return [classify_issue_refs(pr.get("body")) for pr in prs]


def parse_pr_refs_from_issue(text: str | None, known_prs: set[int]) -> set[int]:
    """PR numbers the issue's own text points at, kept only when the number is a
    PR in `known_prs` (a reference to an unknown number links nothing)."""
    out: set[int] = set()
    for m in ISSUE_PR_REF.finditer(text or ""):
        n = int(next(g for g in m.groups() if g))
        if n in known_prs:
            out.add(n)
    return out


def candidate_prs(issue_number: int, issue_subsystem: str | None, prs: list[dict],
                  refs: list[dict[int, str]] | None = None,
                  issue_text: str | None = None) -> list[dict]:
    """The PRs that may address this issue, strongest evidence per PR: an explicit
    Fixes/Closes/Resolves reference in the PR body ("explicit", any PR state),
    another #N in the body ("body-ref", any PR state), a PR named in the issue's
    own text ("issue-ref", any PR state), else a shared (non-"other") subsystem
    ("subsystem", open PRs only — a tag-match only means anything for a PR that
    could still land). Every direct match is kept; "subsystem" matches are capped
    at SUBSYSTEM_CAP so a broad subsystem can't bloat the stored list. `refs` is
    the positional parse result; when omitted it is computed here. A PR without a
    "state" key counts as open."""
    if refs is None:
        refs = parse_refs(prs)
    issue_refs = parse_pr_refs_from_issue(issue_text, {pr["number"] for pr in prs})
    direct, subsystem = [], []
    for pr, pr_refs in zip(prs, refs):
        if how := pr_refs.get(issue_number):
            direct.append({"pr": pr["number"], "how": how, "title": pr.get("title", "")})
        elif pr["number"] in issue_refs:
            direct.append({"pr": pr["number"], "how": "issue-ref", "title": pr.get("title", "")})
        elif pr.get("state", "open") == "open" and pr.get("subsystem") \
                and pr["subsystem"] == issue_subsystem and issue_subsystem != "other":
            subsystem.append({"pr": pr["number"], "how": "subsystem", "title": pr.get("title", "")})
    return direct + subsystem[:SUBSYSTEM_CAP]
