"""The ONE implementation of 'is this fact still about the current code?'.

A fact section is current iff it exists, was computed against the PR's
current meta.head_sha, and (when a window is given) is younger than
max_age_days.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.storekit import currency_failure_core, is_current_core

if TYPE_CHECKING:
    from pipeline.model import Pr

# sections whose facts are tied to the code at a specific head.
# `cluster` is sha-bound so a clustering-attempt stamp distinguishes the two
# clusterless states: a current stamp with no `id` is "a pass considered this PR
# and left it standalone"; an absent or stale stamp is "no pass has reached it
# (or its head moved since)". A clustered PR carries `id` and is treated as
# clustered regardless of freshness — membership persists across head moves.
SHA_BOUND = ("signals", "drift", "summary", "cluster", "analysis", "security",
             "greptile_review", "verify")

# Sections whose producer logic/schema can change without the PR's head moving
# (a classifier edit, not a code edit). Each such section carries the version it
# was produced under; bump the number here to mark every existing instance stale
# and have the next wave re-produce it. Human-controlled — most edits don't merit
# a bump. (summary 2: dominant-intent primary_change/secondary_changes schema.)
SECTION_SCHEMA_VERSION = {"summary": 2}


def is_current(pr: Pr, section: str, max_age_days: int | None = None,
               today: str | None = None) -> bool:
    sec = pr.section(section)
    token_field = "against_head_sha" if section in SHA_BOUND else None
    token_value = pr.head_sha if token_field else None
    return is_current_core(sec, token_field, token_value,
                           SECTION_SCHEMA_VERSION.get(section), max_age_days, today)


def currency_failure(pr: Pr, section: str, max_age_days: int | None = None,
                     today: str | None = None) -> str | None:
    """Why `section` fails is_current, as a short human phrase — None when current.

    The reason-bearing complement of is_current (same checks, same parameters),
    for gate reasons and banners that must say *which* check failed: 'missing',
    'stale (computed against an earlier head)', or '12d old, outside the 7d
    window'."""
    sec = pr.section(section)
    token_field = "against_head_sha" if section in SHA_BOUND else None
    token_value = pr.head_sha if token_field else None
    return currency_failure_core(sec, token_field, token_value,
                                 SECTION_SCHEMA_VERSION.get(section), max_age_days, today)


def stale_sections(pr: Pr) -> list[str]:
    """Sections present on the record but no longer current (head moved)."""
    return [s for s in SHA_BOUND if pr.section(s) and not is_current(pr, s)]
