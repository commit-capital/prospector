"""Closing-comment templates.

``default_comment`` is the shared source of truth for triage closing-comment
wording — the executor falls back to it when an action carries no explicit
comment, and ``suggest.py`` / the ``/api/default-comment`` preview use the same
text.
"""
from __future__ import annotations

from pipeline import settings
from review_cockpit.backend import models


# A few equivalent, honest phrasings for a duplicate close, so a queue of
# dupe-closes doesn't read as the same boilerplate line every time (#184). Keyed
# deterministically on the canonical PR number, so the panel preview matches what
# the executor posts. None of these assert *why* the canonical is preferred —
# that claim is only made when a genuine `dup_reason` is supplied.
_DUP_OPENERS = (
    "Thanks for the contribution! This change is already covered by #{c}{reason}.",
    "Thanks for digging into this! The same change already landed in #{c}{reason}.",
    "Appreciate the PR! This is already handled by #{c}{reason}.",
)
_DUP_CLOSERS = (
    "Closing as a duplicate — your work helped validate the approach.",
    "Closing as a duplicate. Thanks for helping confirm the fix.",
    "Closing this out as a duplicate — thanks for the effort here.",
)


def default_comment(a: models.CloseAction) -> str:
    """Closing-comment templates mirroring /resolve-pr-cluster's wording."""
    act = a.override_action or a.action
    canonical = a.canonical
    if act == "CLOSE_DUP" and canonical:
        i = canonical % len(_DUP_OPENERS)
        # Only state *why* the canonical wins when a real, specific reason is
        # given — never default to "broader test coverage" boilerplate (#184).
        raw = (a.dup_reason or "").strip().rstrip(".")
        reason = f", {raw}" if raw else ""
        return f"{_DUP_OPENERS[i].format(c=canonical, reason=reason)} {_DUP_CLOSERS[i]}"
    if act == "CLOSE_DUP":  # no canonical given → neutral close
        return "Thanks for the contribution! Closing as a duplicate during triage."
    if act == "CLOSE_FIXED":
        # Every change lands on the default branch through a squash-merged PR, so
        # the PR number is the canonical, clickable, verifiable citation — cite
        # that, not a commit hash (a squash-merge collapses the PR's branch
        # commits, so a hash the agent picked off the branch may not even exist
        # in the default branch's history).
        branch = settings.default_branch()
        if (upstream_pr := a.upstream_pr):
            cite = f"#{upstream_pr}"
            if (merged := a.upstream_date):
                cite += f", merged {merged}"
            return (f"Thanks for the contribution! This fix already landed on {branch} in "
                    f"{cite}, which applies the same fix at this call site. Closing as "
                    "already-fixed — your investigation helped confirm this was a widespread issue.")
        # no upstream PR to cite — DON'T fabricate one; hedge honestly
        return (f"Thanks for the contribution! It looks like an equivalent fix has already landed on "
                f"{branch}, so this change appears to be redundant. Closing as already-fixed during "
                "triage — please reopen if you believe this is still needed.")
    if act == "CLOSE":
        return "Thanks for the contribution! Closing this PR during triage."
    if act == "CLOSE_STALE":
        return ("Thanks for the contribution! This PR has been inactive for a while and has drifted from the "
                "current codebase. Closing during triage to keep the queue manageable — please reopen or resubmit "
                "if it's still relevant.")
    if act == "CLOSE_OVERSIZED":
        # Encourage splitting an over-scoped PR, and — when it's part of a cluster
        # we're already merging from — point at those PRs so the author doesn't
        # resubmit the same work (#183).
        merge_prs = [p for p in (a.merge_prs or []) if p]
        keep = ""
        if merge_prs:
            refs = ", ".join(f"#{p}" for p in merge_prs)
            keep = (f" Heads-up: we're already merging {refs} from this effort, so no need to "
                    "resubmit those parts — focus the smaller PRs on what's left.")
        return ("Thanks for the contribution! This PR bundles a lot of distinct changes into one, which makes "
                "it hard to review and slow to land. Please break it into smaller, self-contained PRs — each "
                "focused on a single change is far more likely to get merged quickly." + keep +
                " Closing for now; we'd genuinely welcome the split-up PRs.")
    return ""


