"""The ONE implementation of 'is this fact still about the current code?'.

A fact section is current iff it exists, was computed against the PR's
effective head — the head last observed upstream, else the ingested one — and
(when a window is given) is younger than max_age_days. A fact about the change
itself (CARRIED) also stays current at a head whose diff is the same patch, as
after merging the base into the branch: the threat scan records which earlier
heads those are in the `patch` section.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.storekit import currency_failure_core

if TYPE_CHECKING:
    from pipeline.model import Pr

# sections whose facts are tied to the code at a specific head.
# `cluster` is sha-bound so a clustering-attempt stamp distinguishes the two
# clusterless states: a current stamp with no `id` is "a pass considered this PR
# and left it standalone"; an absent or stale stamp is "no pass has reached it
# (or its head moved since)". A clustered PR carries `id` and is treated as
# clustered regardless of freshness — membership persists across head moves.
# `threat` is sha-bound for its currency alone: the gates read a malicious
# verdict at any head, so a flag stays sticky however far the head moves.
SHA_BOUND = ("signals", "reviews", "drift", "summary", "cluster", "analysis", "security",
             "greptile_review", "verify", "threat")

# The sha-bound facts judged from the diff alone. CI, mergeability, reviewer
# feeds and drift belong to the exact commit; the threat scan judges every head,
# since it is what records the `patch` section.
CARRIED = ("summary", "cluster", "analysis", "security", "verify")

# The stale shape a re-run alone cannot fix: the fact is about the head we
# ingested, and the PR has moved past it upstream. Re-ingest first, then re-run.
UNINGESTED_PUSH = "stale (new commits upstream, not yet ingested)"

# Sections whose producer logic/schema can change without the PR's head moving
# (a classifier edit, not a code edit). Each such section carries the version it
# was produced under; bump the number here to mark every existing instance stale
# and have the next wave re-produce it. Human-controlled — most edits don't merit
# a bump. (summary 2: dominant-intent primary_change/secondary_changes schema.)
SECTION_SCHEMA_VERSION = {"summary": 2}


def upstream_head_moved(pr: Pr) -> bool:
    """True when the last live observation found a head that has not been
    ingested. Every sha-bound fact is computed against the ingested head, so all
    of them read stale until an ingest catches up."""
    live = pr.live_head_sha
    return bool(live and pr.head_sha and live != pr.head_sha)


def same_patch(pr: Pr, sha: str | None) -> bool:
    """Whether a fact computed against head `sha` describes `pr`'s current
    diff: `sha` is the effective head, or an earlier head the `patch` section
    at the ingested head records as the same patch."""
    if not sha:
        return False
    if sha == pr.effective_head_sha:
        return True
    patch = pr.section("patch") or {}
    return (not upstream_head_moved(pr) and patch.get("against_head_sha") == pr.head_sha
            and sha in patch.get("same_as", ()))


def _token(pr: Pr, section: str, sec: dict | None) -> str | None:
    """The head `section` must be stamped against to read current."""
    sha = (sec or {}).get("against_head_sha")
    return sha if section in CARRIED and same_patch(pr, sha) else pr.effective_head_sha


def is_current(pr: Pr, section: str, max_age_days: int | None = None,
               today: str | None = None) -> bool:
    return currency_failure(pr, section, max_age_days, today) is None


def currency_failure(pr: Pr, section: str, max_age_days: int | None = None,
                     today: str | None = None) -> str | None:
    """Why `section` fails is_current, as a short human phrase — None when current.

    The reason-bearing complement of is_current (same checks, same parameters),
    for gate reasons and banners that must say *which* check failed: 'missing',
    'stale (computed against an earlier head)', UNINGESTED_PUSH, or '12d old,
    outside the 7d window'. A fact that matches the ingested head reads as the
    uningested push rather than an earlier head, because the operator's next step
    is an ingest, not a re-run."""
    sec = pr.section(section)
    token_field = "against_head_sha" if section in SHA_BOUND else None
    token_value = _token(pr, section, sec) if token_field else None
    why = currency_failure_core(sec, token_field, token_value,
                                SECTION_SCHEMA_VERSION.get(section), max_age_days, today)
    if (why is not None and token_field is not None and sec is not None
            and upstream_head_moved(pr) and sec.get(token_field) == pr.head_sha):
        return UNINGESTED_PUSH
    return why


def stale_sections(pr: Pr) -> list[str]:
    """Sections present on the record but no longer current (head moved)."""
    return [s for s in SHA_BOUND if pr.section(s) and not is_current(pr, s)]
