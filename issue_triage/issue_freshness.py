"""The ONE 'is this issue-fact still about the current issue?' check.

An issue fact is current iff it exists, was computed against the issue's current
meta.updated_at (GitHub bumps updated_at on edit/comment/label/state change — the
issue-side analog of a PR's head_sha), matches its schema version, and is within
any max-age window. The comparison engine is pipeline/storekit.is_current_core,
shared with the PR freshness check.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.storekit import is_current_core

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue

# Sections whose facts are tied to the issue's content/thread at a specific
# updated_at. `cluster` is included so an absent/stale stamp distinguishes the two
# clusterless states; a clustered issue carries an id and is treated as clustered
# regardless of freshness (membership persists across updates). `links` is NOT
# here — it is recomputed whenever ingest rewrites an issue (i.e. when its
# meta/summary/repro move), against whatever PRs currently exist.
UPDATED_BOUND = ("summary", "repro", "cluster", "analysis", "fix_scan")

# Per-section producer-logic version; bump to mark every existing instance stale.
SECTION_SCHEMA_VERSION: dict[str, int] = {}


def is_current(issue: Issue, section: str, max_age_days: int | None = None,
               today: str | None = None) -> bool:
    sec = issue.section(section)
    token_field = "against_updated_at" if section in UPDATED_BOUND else None
    token_value = issue.updated_at if token_field else None
    return is_current_core(sec, token_field, token_value,
                           SECTION_SCHEMA_VERSION.get(section), max_age_days, today)
