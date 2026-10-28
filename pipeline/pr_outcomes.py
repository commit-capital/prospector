"""Where a PR lane's run stopped (`pipeline.outcomes`), from what the lane
recorded: the design review, the security review, verification, and PR
autofix."""
from __future__ import annotations

from pipeline import capacity, gates, outcomes
from pipeline.outcomes import Fault, Outcome

DESIGN = "design-review"
SECURITY = "security-review"
VERIFY = "verify"
AUTOFIX = "autofix"


def _pr(n: int, sha: str | None) -> outcomes.Subject:
    return {"kind": "pr", "number": n, "sha": sha or None}


def design(n: int, head: str | None, stage: str, words: str = "") -> Outcome:
    """`stage` is the review's ending: `reviewed`, `blocking` (it found
    standing blocking findings), or a machine's: `coverage`, `no-diff`,
    `agent`, `error`."""
    fault: Fault | None = {"reviewed": None, "blocking": "judged"}.get(stage, "machine")
    return outcomes.make(DESIGN, _pr(n, head), stage, fault=fault, words=words)


def security(n: int, head: str | None, stage: str, words: str = "") -> Outcome:
    """`stage` is the verdict, lowercased, or a machine's ending: `held`,
    `no-diff`, `agent`, `error`."""
    fault: Fault | None = {"green": None, "yellow": "judged", "red": "judged"}.get(
        stage, "machine")
    return outcomes.make(SECURITY, _pr(n, head), stage, fault=fault, words=words)


def verify(n: int, head: str | None, *, status: str | None, error_kind: str | None,
           error: str | None, outcome: str | None) -> Outcome | None:
    """A verify run's ending, or None for one that reached no ending (a
    cancel)."""
    if status == "done":
        if outcome == "verified-fix":
            return outcomes.make(VERIFY, _pr(n, head), "verified", fault=None)
        return outcomes.make(VERIFY, _pr(n, head), "concluded", fault="judged",
                             words=outcome or "")
    if not error_kind or error_kind not in outcomes.STAGES[VERIFY]:
        return None
    fault: Fault = "machine" if gates.VERIFY_REQUEST_FAULT.get(error_kind) == "system" else "subject"
    return outcomes.make(VERIFY, _pr(n, head), error_kind, fault=fault, words=error or "")


def autofix(n: int, head: str | None, *, action: str, status: str, detail: str | None,
            kind: str | None, judged: bool | None) -> Outcome | None:
    """A fix request's ending, or None for a deferral until AI capacity opens."""
    words = f"{action}: {detail}" if detail else action
    if status == "failed":
        if kind == capacity.PAUSED_KIND:
            return None
        stage = kind if kind in outcomes.STAGES[AUTOFIX] else "run-failed"
        return outcomes.make(AUTOFIX, _pr(n, head), stage, fault="machine", words=words)
    if status == "refused":
        return outcomes.make(AUTOFIX, _pr(n, head), "refused",
                             fault="judged" if judged else "subject", words=words)
    stage = {"awaiting-review": "parked"}.get(status, status)
    if stage not in outcomes.STAGES[AUTOFIX]:
        return None
    return outcomes.make(AUTOFIX, _pr(n, head), stage, fault=None, words=words)
