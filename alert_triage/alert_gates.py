"""The ONE policy module for alert actions.

`dismiss_eligibility` gates every upstream alert dismissal/resolution. It is
fail-closed: a closed alert, an unknown reason for the source, a
fixed-evidence reason without a current supporting verdict, or a judgment
reason without an operator note all refuse with the human-readable why.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from alert_triage.alert_freshness import FIX_SCAN_MAX_AGE_DAYS, is_current

if TYPE_CHECKING:
    from alert_triage.alert_model import Alert

# The exact reason vocabulary each source's PATCH endpoint accepts.
DISMISS_REASONS: dict[str, set[str]] = {
    "code-scanning": {"false positive", "won't fix", "used in tests"},
    "dependabot": {"fix_started", "inaccurate", "no_bandwidth", "not_used",
                   "tolerable_risk"},
    "secret-scanning": {"revoked", "false_positive", "wont_fix", "used_in_tests"},
}

# Reasons that assert the alert is handled by a fix — they require a current
# fixed/likely-fixed verdict as evidence. Every other reason is a human
# judgment call and requires an operator note instead.
FIXED_EVIDENCE_REASONS = {"fix_started", "revoked"}


def dismiss_eligibility(alert: Alert, reason: str, note: str,
                        today: str | None = None) -> tuple[bool, str]:
    """Whether this alert may be dismissed upstream with `reason`, and why not.
    `note` is the operator's comment; `today` is injectable for tests."""
    if alert.state != "open":
        return False, f"alert is {alert.state}, not open"
    valid = DISMISS_REASONS.get(alert.source, set())
    if reason not in valid:
        return False, (f"reason {reason!r} is not valid for {alert.source} "
                       f"(valid: {sorted(valid)})")
    if reason in FIXED_EVIDENCE_REASONS:
        if not is_current(alert, "fix_scan", max_age_days=FIX_SCAN_MAX_AGE_DAYS,
                          today=today):
            return False, ("a fixed-evidence dismissal needs a current fix-scan "
                           "verdict; run the find-fixed pass first")
        if alert.verdict not in ("fixed", "likely-fixed"):
            return False, (f"a fixed-evidence dismissal needs a fixed/likely-fixed "
                           f"verdict; the current verdict is {alert.verdict!r}")
        return True, ""
    if not note.strip():
        return False, "a judgment dismissal needs an operator note"
    return True, ""
