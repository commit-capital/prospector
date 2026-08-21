"""The ONE 'is this fact still about the current alert or advisory?' check.

A record's fact is current iff it exists, was computed against the record's
current meta.updated_at (GitHub bumps updated_at on state changes and new
instances), and is within any max-age window. The comparison engine is
pipeline/storekit.is_current_core, shared with the PR and issue freshness
checks. The fixed-pass additionally bounds fix_scan by FIX_SCAN_MAX_AGE_DAYS:
alerts and advisories are fixed by the *repository* moving, so a verdict
refreshes on age even while the record itself is quiet.
"""
from __future__ import annotations

from typing import Protocol

from pipeline.storekit import is_current_core


class Stamped(Protocol):
    """A store record whose fact sections carry `against_updated_at`."""

    def section(self, name: str) -> dict | None: ...

    @property
    def updated_at(self) -> str | None: ...

# Sections whose facts are tied to the record's content at a specific updated_at.
UPDATED_BOUND = ("links", "fix_scan")

# Per-section producer-logic version; bump to mark every existing instance stale.
SECTION_SCHEMA_VERSION: dict[str, int] = {}

FIX_SCAN_MAX_AGE_DAYS = 7


def is_current(alert: Stamped, section: str, max_age_days: int | None = None,
               today: str | None = None) -> bool:
    sec = alert.section(section)
    token_field = "against_updated_at" if section in UPDATED_BOUND else None
    token_value = alert.updated_at if token_field else None
    return is_current_core(sec, token_field, token_value,
                           SECTION_SCHEMA_VERSION.get(section), max_age_days, today)
