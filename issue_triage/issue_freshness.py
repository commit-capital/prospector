"""The ONE 'is this issue-fact still about the current issue?' check.

Every UPDATED_BOUND fact is stamped with the issue's meta.updated_at when it was
computed (`against_updated_at`). GitHub bumps updated_at on any change, a label,
an assignment or the bot's own comment among them, so the stamp is read against
the issue's content time: meta.content_updated_at, which ingest sets to the
issue's last material change — its creation, an edit to its title or body, a
reopen, or a comment by someone other than the bot or another GitHub App
(fetch_issues._content_updated_at). A fact is current iff it exists, its stamp
is at or after the content time, it matches its schema version, and it is within
any max-age window. An issue whose meta carries no content time is held to the
exact rule: the stamp must equal meta.updated_at. The existence, version and age
checks are pipeline/storekit.is_current_core, shared with the PR freshness
check.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.storekit import is_current_core, parse_ts

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue

# Sections whose facts are tied to the issue's content and thread as of a stamped
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
    version = SECTION_SCHEMA_VERSION.get(section)
    if section not in UPDATED_BOUND:
        return is_current_core(sec, None, None, version, max_age_days, today)
    material = parse_ts(issue.content_updated_at)
    if material is None:
        return is_current_core(sec, "against_updated_at", issue.updated_at,
                               version, max_age_days, today)
    stamp = parse_ts((sec or {}).get("against_updated_at"))
    return (stamp is not None and stamp >= material
            and is_current_core(sec, None, None, version, max_age_days, today))
