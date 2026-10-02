"""The pull requests on TRIAGE_REPO that already name an issue — the search a
proposed fix's description affirms it made, and the duplicate check before a
proposal opens.

`search` asks GitHub's search for pull requests, open and closed, whose title or
body names the issue's number, read as the operator, and marks each whose body
claims to fix the issue (`link_prs.parse_issue_refs`: Fixes / Closes / Resolves).
None means the search did not answer, which is not the same as finding nothing:
a description affirms the search only when it ran.
"""
from __future__ import annotations

from typing import TypedDict
from urllib.parse import quote

from issue_triage import link_prs
from pipeline import gh, settings


class RelatedPr(TypedDict):
    number: int
    title: str
    state: str  # open | closed | merged
    author: str | None
    closes: bool


def search(issue: int, *, exclude: set[int] | None = None) -> list[RelatedPr] | None:
    """Pull requests naming #`issue`, newest first, less `exclude`; None when the
    search could not run."""
    q = quote(f"repo:{settings.repo()} is:pr {int(issue)} in:title,body")
    doc = gh.gh_json(f"search/issues?q={q}&sort=created&order=desc&per_page=50")
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        return None
    out: list[RelatedPr] = []
    for item in doc["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("number"), int):
            continue
        if item["number"] in (exclude or set()):
            continue
        merged = bool((item.get("pull_request") or {}).get("merged_at"))
        out.append({"number": item["number"], "title": str(item.get("title") or ""),
                    "state": "merged" if merged else str(item.get("state") or "closed"),
                    "author": (item.get("user") or {}).get("login"),
                    "closes": int(issue) in link_prs.parse_issue_refs(item.get("body"))})
    return out
