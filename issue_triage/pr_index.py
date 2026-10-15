"""Issue → the pull requests whose own bodies link it, inverted from the PR
store's `issues.linked` sections. Every PR ingest path refreshes those from live
PR bodies, so this index is as fresh as the PR store itself."""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from pipeline.model import Pr

# The link kinds a PR's own body establishes.
DIRECT = ("explicit", "body-ref")

# The PR states that link an issue: an open PR is pending work on it and a
# merged one is evidence it is fixed — the corpus issue_ingest matches against.
LINKING_STATES = ("open", "merged")


class PrLink(TypedDict):
    pr: int
    how: str
    state: str | None
    draft: bool
    title: str | None


def build(prs: Iterable[Pr]) -> dict[int, list[PrLink]]:
    """Issue number → its direct links from open and merged PRs, one per PR
    (explicit over body-ref), ordered by PR number."""
    return _index((pr.n, pr.state, pr.draft, pr.title, pr.linked_issues) for pr in prs)


def _index(rows: Iterable[tuple[int, str | None, bool, str | None, list]]
           ) -> dict[int, list[PrLink]]:
    """`build` over (number, state, draft, title, issues.linked) rows."""
    best: dict[tuple[int, int], PrLink] = {}
    for n, state, draft, title, linked in rows:
        if state not in LINKING_STATES:
            continue
        for entry in linked:
            how = entry.get("how")
            if how not in DIRECT or entry.get("issue") is None:
                continue
            key = (int(entry["issue"]), n)
            if key in best and best[key]["how"] == "explicit":
                continue
            best[key] = {"pr": n, "how": how, "state": state, "draft": draft,
                         "title": title}
    out: dict[int, list[PrLink]] = {}
    for (issue, _), link in sorted(best.items()):
        out.setdefault(issue, []).append(link)
    return out


def from_store() -> dict[int, list[PrLink]]:
    """The index over the PR store, read as the five fields it needs from the
    open and merged PRs only (`Store.pr_rows`), never the PR records."""
    from pipeline.store import Store
    rows = Store().pr_rows([("meta", "draft"), ("issues", "linked")], states=LINKING_STATES)
    return _index((row["pr"], row["state"], bool(draft), row["title"],
                   linked if isinstance(linked, list) else [])
                  for row, (draft, linked) in rows)
