"""The ONE review/merge-provider policy.

Which external code-review provider (if any) gates a clean merge, and at what bar.
Two built-in profiles selected by TRIAGE_REVIEW_PROVIDER: `greptile` reproduces the
confidence-score bar; `none` requires no external review. Every consumer — the
gate, the ANALYZE prompt, the cockpit capabilities descriptor — reads the active
policy here rather than a literal provider name or threshold.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pipeline import settings

if TYPE_CHECKING:
    from pipeline.model import Pr


@dataclass(frozen=True)
class ReviewPolicy:
    provider: str                  # "greptile" | "none"
    label: str                     # display name; "" when no provider
    required: bool                 # does a passing score gate pr_clean?
    threshold: int | None          # exact pass score, e.g. 5
    score_max: int | None          # score denominator, e.g. 5
    section: str | None            # store section holding the review digest
    retrigger_mention: str | None  # comment body that re-triggers a review

    def clean_blocker(self, pr: Pr) -> str | None:
        """The reason this PR fails the review bar, or None when it passes (or no
        review is required). The caller has already confirmed signals are current;
        the score is compared exactly to the threshold."""
        if not self.required:
            return None
        score = pr.review_score
        if score != self.threshold:
            shown = score if score is not None else "?"
            return f"{self.label.lower()} {shown}/{self.score_max}"
        return None


_GREPTILE = ReviewPolicy(
    provider="greptile", label="Greptile", required=True, threshold=5, score_max=5,
    section="greptile_review", retrigger_mention="@greptileai",
)

_NONE = ReviewPolicy(
    provider="none", label="", required=False, threshold=None, score_max=None,
    section=None, retrigger_mention=None,
)

_PROFILES: dict[str, ReviewPolicy] = {"greptile": _GREPTILE, "none": _NONE}


def active() -> ReviewPolicy:
    """The configured review policy. Reads settings on each call (cheap) so tests
    can monkeypatch settings.REVIEW_PROVIDER without cache invalidation."""
    base = _PROFILES[settings.REVIEW_PROVIDER]
    if base.required and settings.REVIEW_THRESHOLD is not None:
        return replace(base, threshold=settings.REVIEW_THRESHOLD)
    return base
