"""Named transforms for pipeline.store_edit — reviewed, reusable record edits,
run as `uv run python -m pipeline.store_edit pipeline.edits:<name>` (dry-run by
default; add --live to apply)."""
from __future__ import annotations

# The rationale templates a stored verify/security route writes into the
# analysis section (gates.verify_disposition / gates.security_disposition).
_FORCED_RATIONALE_PREFIXES = (
    "Security review is RED — ",
    "Security review is YELLOW — ",
    "Dynamic verification escalated — ",
    "Dynamic verification refused — ",
    "Dynamic verification could not apply this PR to the pinned main.",
    "Dynamic verification did not confirm the fix — ",
    "Dynamic verification found a regression — ",
)

RESTORED_RATIONALE = ("Merge pick — restored from a stored verify/security "
                      "route that overwrote the analyze rationale.")


def restore_demoted_merge_picks(rec: dict) -> dict | None:
    """Restore an analysis section that holds a stored verify/security route to
    the merge verdict ANALYZE produced. Only merge picks are ever
    security-reviewed or verified, so a record whose analysis rationale is one
    of the forced-route templates was a merge pick. The route derives on read
    (gates.merge_demotion): a still-blocked record reads the same after the
    restore, and one whose fact has cleared heals to merge."""
    a = rec.get("analysis")
    if not a or a.get("disposition") not in ("request-changes", "needs-human"):
        return None
    rationale = a.get("rationale") or ""
    if not rationale.startswith(_FORCED_RATIONALE_PREFIXES):
        return None
    a["disposition"] = "merge"
    a["rationale"] = RESTORED_RATIONALE
    return rec
