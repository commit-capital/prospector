"""An issue's linked PRs, merged from the three sources that know about them:
the PR index over live PR bodies, GitHub's own closing references, and the
issue's stored candidates. One entry per PR under its strongest evidence."""
from __future__ import annotations

from typing import TYPE_CHECKING

from issue_triage import pr_index

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue

# Evidence strength, strongest first. A PR body's "Fixes #N" and GitHub's own
# closing reference are the same claim from two mouths, so they tie.
HOW_RANK: dict[str, int] = {"explicit": 0, "github": 0, "fix-found": 1,
                            "issue-ref": 2, "body-ref": 3, "subsystem": 4}

# A kind no rank names sorts after every known one.
UNRANKED = 5

# The kinds where something names the PR and the issue together. A subsystem tag
# and a bare #N in a PR body are weaker: neither claims the PR addresses this issue.
REFERENCED = frozenset({"explicit", "github", "fix-found", "issue-ref"})


def how_rank(cand: dict) -> int:
    return HOW_RANK.get(cand.get("how") or "", UNRANKED)


def referenced(cand: dict) -> bool:
    return cand.get("how") in REFERENCED


def linked_prs(issue: Issue, pr_links: list[pr_index.PrLink] | None) -> list[dict]:
    """Every PR linked to `issue`, one entry per PR under its strongest kind,
    ordered strongest first. `pr_links` is the PR index's entries for this issue;
    the index owns the kinds a PR body establishes, so a list (even an empty one)
    drops the stored `explicit` / `body-ref` candidates, while `None` — the index
    unavailable — keeps the stored snapshot. Entries whose source knows the PR's
    live state carry `state` and `draft` alongside `pr`, `how`, and `title`."""
    by_pr: dict[int, dict] = {}

    def offer(entry: dict) -> None:
        held = by_pr.get(entry["pr"])
        if held is None or how_rank(entry) < how_rank(held):
            by_pr[entry["pr"]] = entry

    for link in pr_links or []:
        offer({"pr": int(link["pr"]), "how": link["how"], "title": link["title"] or "",
               "state": link["state"], "draft": bool(link["draft"])})
    for cand in issue.candidate_prs:
        if cand.get("pr") is None or (pr_links is not None and cand.get("how") in pr_index.DIRECT):
            continue
        offer({"pr": int(cand["pr"]), "how": cand.get("how"), "title": cand.get("title") or ""})
    for ref in issue.github_links:
        if ref.get("pr") is None:
            continue
        offer({"pr": int(ref["pr"]), "how": "github", "title": "",
               "state": ref.get("state"), "draft": bool(ref.get("draft"))})
    return sorted(by_pr.values(), key=lambda cand: (how_rank(cand), cand["pr"]))
