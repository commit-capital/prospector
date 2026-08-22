"""The ONE review/merge-provider policy: which automated reviewers and scanners
gate a clean merge on this repository, and at what bar.

`TRIAGE_REVIEW_PROVIDER` is `auto` (every registry reviewer seen on an open PR
within `TRIAGE_REVIEWER_ACTIVE_DAYS`, read from the store's `reviewers`
registry that ingest recomputes), `none`, or an explicit comma list of reviewer
ids. Every consumer — the gates, the checks rollup, the prompts, the app
capabilities — reads the active set and each reviewer's bar here."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pipeline import reviewers, settings
from pipeline.reviewers import Bar, Reviewer

if TYPE_CHECKING:
    from pipeline.model import Pr

_SEEN_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class ReviewPolicy:
    mode: str                       # "auto" | "none" | "explicit"
    explicit: tuple[str, ...]
    active_days: int
    threshold: int | None           # Greptile score bar override


@dataclass(frozen=True)
class Blocker:
    reviewer: Reviewer
    bar: Bar


def policy() -> ReviewPolicy:
    mode, ids = settings.review_provider()
    return ReviewPolicy(mode, ids, settings.reviewer_active_days(), settings.review_threshold())


_seen_cache: tuple[float, dict] | None = None


def reset() -> None:
    """Drop the cached activity registry (a reconfigure or a test reset)."""
    global _seen_cache
    _seen_cache = None


def _load_seen() -> dict:
    from pipeline.store import Store
    return dict(Store().load_reviewers().get("seen") or {})


def _seen() -> dict:
    global _seen_cache
    now = time.monotonic()
    if _seen_cache is None or now - _seen_cache[0] > _SEEN_TTL_SECONDS:
        try:
            _seen_cache = (now, _load_seen())
        except Exception:
            _seen_cache = (now, {})
    return _seen_cache[1]


def _today() -> str:
    return date.today().isoformat()


def _recent(stamp: str | None, days: int) -> bool:
    if not stamp:
        return False
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    today = datetime.fromisoformat(_today()).replace(tzinfo=timezone.utc)
    return at >= today - timedelta(days=days)


def active_reviewers(kind: str | None = None) -> list[Reviewer]:
    """The reviewers that gate this repository, in registry order."""
    p = policy()
    if p.mode == "none":
        ids: list[str] = []
    elif p.mode == "explicit":
        ids = list(p.explicit)
    else:
        seen = _seen()
        ids = [rid for rid in reviewers.REVIEWERS
               if _recent((seen.get(rid) or {}).get("last_observed_at"), p.active_days)]
    out = [r for rid, r in reviewers.REVIEWERS.items() if rid in ids]
    return [r for r in out if kind is None or r.kind == kind]


def is_active(reviewer_id: str) -> bool:
    return any(r.id == reviewer_id for r in active_reviewers())


def bar(pr: Pr, reviewer: Reviewer) -> Bar:
    """`reviewer`'s bar on `pr`; `na` when the reviewer is not active."""
    if not is_active(reviewer.id):
        return Bar(reviewers.NA, None, None)
    return reviewers.bar(reviewer, pr.review_entry(reviewer.id), pr.head_sha,
                         threshold=policy().threshold)


def clean_blockers(pr: Pr, kind: str) -> list[Blocker]:
    """Every active reviewer of `kind` whose bar is not pass/na, in registry order."""
    out: list[Blocker] = []
    for r in active_reviewers(kind):
        b = bar(pr, r)
        if b.status not in (reviewers.PASS, reviewers.NA):
            out.append(Blocker(r, b))
    return out


def greptile_threshold() -> int:
    th = policy().threshold
    return th if th is not None else (reviewers.GREPTILE.score_max or 5)


def bar_label(reviewer: Reviewer) -> str:
    """The reviewer's pass condition as prose."""
    if reviewer is reviewers.GREPTILE:
        return f"{reviewer.label} at {greptile_threshold()}/{reviewer.score_max}"
    if reviewer is reviewers.CODERABBIT:
        return f"{reviewer.label} with no open Critical/Major findings"
    if reviewer is reviewers.SUPERAGENT:
        return f"{reviewer.label} scan clean (no open P1/P2)"
    return f"{reviewer.label} with no new dependency alerts"


def merge_bar_sentence() -> str:
    """The hard merge bar, as the ANALYZE and chat prompts state it."""
    parts = [bar_label(r) for r in active_reviewers(reviewers.REVIEW)]
    parts += [bar_label(r) for r in active_reviewers(reviewers.SCANNER)]
    if parts:
        return "external review: " + ", ".join(parts) + "; CI passing, mergeable (no conflicts)"
    return "CI passing, mergeable (no conflicts)"


def describe() -> list[dict]:
    """The capabilities descriptor: every registry reviewer with its activity."""
    active = {r.id for r in active_reviewers()}
    out: list[dict] = []
    for r in reviewers.REVIEWERS.values():
        out.append({"id": r.id, "label": r.label, "kind": r.kind, "active": r.id in active,
                    "retrigger": r.retrigger_mention is not None, "score_max": r.score_max,
                    "threshold": greptile_threshold() if r is reviewers.GREPTILE else None,
                    "bar_label": bar_label(r)})
    return out
