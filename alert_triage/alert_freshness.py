"""The ONE 'is this alert-fact still about the current alert?' check.

An alert fact is current iff it exists, was computed against the alert's current
meta.updated_at (GitHub bumps updated_at on state changes and new instances),
and is within any max-age window. The comparison engine is
pipeline/storekit.is_current_core, shared with the PR and issue freshness
checks. The fixed-pass additionally bounds fix_scan by FIX_SCAN_MAX_AGE_DAYS:
alerts are fixed by the *repository* moving, so a verdict refreshes on age even
while the alert itself is quiet.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.storekit import is_current_core

if TYPE_CHECKING:
    from alert_triage.alert_model import Alert

# Sections whose facts are tied to the alert's content at a specific updated_at.
UPDATED_BOUND = ("links", "fix_scan")

# Per-section producer-logic version; bump to mark every existing instance stale.
SECTION_SCHEMA_VERSION: dict[str, int] = {}

FIX_SCAN_MAX_AGE_DAYS = 7


def is_current(alert: Alert, section: str, max_age_days: int | None = None,
               today: str | None = None) -> bool:
    sec = alert.section(section)
    token_field = "against_updated_at" if section in UPDATED_BOUND else None
    token_value = alert.updated_at if token_field else None
    return is_current_core(sec, token_field, token_value,
                           SECTION_SCHEMA_VERSION.get(section), max_age_days, today)
