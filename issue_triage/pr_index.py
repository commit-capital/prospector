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


class PrLink(TypedDict):
    pr: int
    how: str
    state: str | None
    draft: bool
    title: str | None
    updated_at: str | None
    head_sha: str | None


def build(prs: Iterable[Pr]) -> dict[int, list[PrLink]]:
    """Issue number → its direct PR links, one per PR (explicit over body-ref),
    ordered by PR number."""
    best: dict[tuple[int, int], PrLink] = {}
    for pr in prs:
        for entry in pr.linked_issues:
            how = entry.get("how")
            if how not in DIRECT or entry.get("issue") is None:
                continue
            key = (int(entry["issue"]), pr.n)
            if key in best and best[key]["how"] == "explicit":
                continue
            best[key] = {"pr": pr.n, "how": how, "state": pr.state, "draft": pr.draft,
                         "title": pr.title, "updated_at": pr.updated_at,
                         "head_sha": pr.head_sha}
    out: dict[int, list[PrLink]] = {}
    for (issue, _), link in sorted(best.items()):
        out.setdefault(issue, []).append(link)
    return out


def from_store() -> dict[int, list[PrLink]]:
    """The index over every PR the PR store holds."""
    from pipeline.store import Store
    return build(Store().all_prs().values())
