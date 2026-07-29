"""The ONE author-leaderboard policy: per-author PR trust stats derived from our
own store. `author_stats` aggregates a PR snapshot by author and folds in the
historical baseline (PRs that closed before our first ingest); `capture_baseline`
materializes that baseline once from a full GitHub enumeration.

Display-only, never a gate input. `merge_rate_shrunk` is the confidence-weighted
trust signal the PR Explorer sorts on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pipeline import gh
from pipeline.settings import REPO_NAME, REPO_OWNER

if TYPE_CHECKING:
    from pipeline.model import Pr
    from pipeline.store import Store

# Beta-Binomial prior for the confidence-weighted merge rate. BASE is the merge
# rate assumed for a brand-new author; K is the pseudo-count — how many decided
# PRs an author needs before their own rate outweighs the prior. K is set for
# triage conservatism (demand a track record before promoting an unknown),
# deliberately above the method-of-moments fit, which collapses because the
# population is bimodal (maintainers merge ~everything, everyone else ~nothing).
_BASE_RATE = 0.12
_PRIOR_K = 4.0


def _blank() -> dict:
    return {"total": 0, "open": 0, "merged": 0, "closed_unmerged": 0, "comments": 0}


def _shrunk(merged: int, decided: int) -> float:
    """Beta-Binomial posterior mean of (merged | decided PRs), shrunk toward the
    base rate by the pseudo-count: a long rejection streak sinks toward 0, a
    genuine unknown (few decided PRs) holds near the base rate."""
    return (merged + _BASE_RATE * _PRIOR_K) / (decided + _PRIOR_K)


def _add_state(acc: dict, state: str | None) -> None:
    acc["total"] += 1
    if state == "merged":
        acc["merged"] += 1
    elif state == "closed":
        acc["closed_unmerged"] += 1
    elif state == "open":
        acc["open"] += 1


def author_stats(baseline: dict, prs: dict[int, Pr]) -> dict[str, dict]:
    """Per-author leaderboard, keyed by lowercased handle: the live store group-by
    (`prs`) field-wise-summed with the historical `baseline` (the inner `authors`
    map). The two are disjoint by construction — the baseline holds only PRs
    absent from the store — so summing needs no de-dup. Each value carries counts,
    `merge_rate`, `merge_rate_shrunk`, `handle`, and `url`."""
    acc: dict[str, dict] = {}
    display: dict[str, str] = {}
    for pr in prs.values():
        handle = pr.author
        if not handle:
            continue
        key = handle.lower()
        display.setdefault(key, handle)
        a = acc.setdefault(key, _blank())
        _add_state(a, pr.state)
        a["comments"] += pr.comments or 0
    for key, b in (baseline or {}).items():
        display.setdefault(key, b.get("handle") or key)
        a = acc.setdefault(key, _blank())
        for f in ("total", "open", "merged", "closed_unmerged", "comments"):
            a[f] += int(b.get(f) or 0)
    out: dict[str, dict] = {}
    for key, a in acc.items():
        decided = a["merged"] + a["closed_unmerged"]
        handle = display[key]
        out[key] = {
            "handle": handle,
            "url": f"https://github.com/{handle}",
            "total": a["total"],
            "open": a["open"],
            "merged": a["merged"],
            "closed_unmerged": a["closed_unmerged"],
            "comments": a["comments"],
            "merge_rate": (a["merged"] / decided) if decided else None,
            "merge_rate_shrunk": _shrunk(a["merged"], decided),
        }
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _page_query(cursor: str | None) -> str:
    after = f'"{cursor}"' if cursor else "null"
    return (f'query {{ repository(owner: "{REPO_OWNER}", name: "{REPO_NAME}") {{ '
            f'pullRequests(first: 100, after: {after}, states: [OPEN CLOSED MERGED], '
            'orderBy: {field: CREATED_AT, direction: ASC}) { '
            'pageInfo { hasNextPage endCursor } '
            'nodes { number state author { login } comments { totalCount } } } } }')


def _enumerate() -> list[dict]:
    """Every PR ever ({number, state, author.login, comments.totalCount}) via
    paginated GraphQL. Stops on the first page that fails to fetch or parse — a
    partial enumeration only under-counts, and the capture is re-runnable."""
    out: list[dict] = []
    cursor: str | None = None
    while True:
        data = gh.gh_graphql(_page_query(cursor))
        conn = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequests")
        if not conn:
            break
        out.extend(conn.get("nodes") or [])
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    return out


def capture_baseline(store: Store) -> dict:
    """Materialize the historical baseline: enumerate every PR, keep only those
    absent from the store (terminal, pre-ingest), aggregate per author, persist.
    Idempotent — re-running recomputes from scratch."""
    present = set(store.all_prs().keys())
    out: dict[str, dict] = {}
    for node in _enumerate():
        n = node.get("number")
        if n is None or int(n) in present:
            continue
        login = (node.get("author") or {}).get("login")
        if not login:
            continue
        a = out.setdefault(login.lower(), {"handle": login, **_blank()})
        _add_state(a, (node.get("state") or "").lower())
        a["comments"] += (node.get("comments") or {}).get("totalCount") or 0
    reg = {"authors": out, "materialized_at": _now()}
    store.save_author_baseline(reg)
    return reg


def main() -> None:
    from pipeline.store import Store
    reg = capture_baseline(Store())
    print(f"author_baseline: {len(reg['authors'])} authors materialized at {reg['materialized_at']}")


if __name__ == "__main__":
    main()
