"""Pain ranking: balanced normalized blend of engagement x severity multiplier.

The key design choice (from brainstorm): duplicates are SIGNAL. A cluster's
reactions and comments are the SUM across all its members, so re-filed
duplicates make the canonical rank higher rather than being discarded. Breadth
is measured by distinct reporters — each author counts once no matter how many
members they filed, so a single prolific filer is one vote, not many. That
ranking drives PR merge priority.
"""
from __future__ import annotations

from typing import Any


def severity_multiplier(text: str, labels: list[str], sev_cfg: dict[str, Any]) -> float:
    t = text.lower()
    hits = sum(1 for kw in sev_cfg["keywords"] if kw in t)
    hits += sum(1 for lb in labels if lb.lower() in sev_cfg["labels"])
    return 1.0 + min(hits * sev_cfg["k"], sev_cfg["cap"])


def percentile_normalize(values: list[int]) -> dict[int, float]:
    """Map each distinct value to its rank fraction in [0,1]; max -> 1.0."""
    uniq = sorted(set(values))
    if len(uniq) == 1:
        return {uniq[0]: 1.0 if uniq[0] else 0.0}
    return {v: i / (len(uniq) - 1) for i, v in enumerate(uniq)}


def pain_for_cluster(reporters: int, reactions: int, comments: int, text: str,
                     labels: list[str], norms: dict[str, dict[int, float]],
                     w: dict[str, Any]) -> float:
    ww = w["weights"]
    base = (ww["reporters"] * norms["reporters"][reporters]
            + ww["reactions"] * norms["reactions"][reactions]
            + ww["comments"] * norms["comments"][comments])
    return round(base * severity_multiplier(text, labels, w["severity"]), 4)
