"""An operator's approval of one pipeline stage past what holds a PR there:
the design review's failing findings, a YELLOW security verdict, or an
escalated verify outcome (`gates.approvable`). It is recorded on that stage's
verdict as `override`, stands at the head the verdict was taken at, and goes
when a new verdict replaces it."""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline import gates, storekit

if TYPE_CHECKING:
    from pipeline.store import Store


def approve(store: Store, n: int, stage: str, head_sha: str, reason: str, *,
            by: str) -> str | None:
    """Record `by`'s approval of PR `n`'s `stage` verdict taken at
    `head_sha`, for `reason`. Returns why it refused, or None."""
    if stage not in gates.APPROVABLE_STAGES:
        return f"{stage!r} is not a stage an operator approves"
    pr = store.load_pr(n)
    if pr is None:
        return f"PR #{n} is not in the store"
    if not reason.strip():
        return "say why you approve it"
    if stage == "security" and pr.security_verdict == "RED":
        return "a RED security verdict is never approvable"
    if gates.approvable(pr, stage) != head_sha:
        return f"the {stage} verdict you read no longer holds this PR; reload it"
    if not store.approve_stage(n, stage, {"reason": reason.strip(), "by": by,
                                          "at": storekit.now()}, head_sha=head_sha):
        return f"the {stage} verdict changed while you approved it; reload the PR"
    return None
