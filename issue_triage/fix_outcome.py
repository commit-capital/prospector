"""Where an issue-fix attempt or a follow-up hand-back stopped
(`pipeline.outcomes`), from what the lane recorded.

`of_run` reads a run's ending and detail, so it serves both a new run's
ledger row and an older row written without an outcome (`of_row`). A fixed
run that covers only part of its report lists what it leaves (`leaves`).
"""
from __future__ import annotations

import re

from issue_triage import issue_gates
from pipeline import outcomes, storekit
from pipeline.outcomes import Asked, Fault, Outcome

# Whose bar a follow-up step answers, besides the code reviewers' labels.
CI = "CI"
MAINTAINER = "a maintainer"

RUN_PHASE = "issue-fix:run"
HANDBACK_PHASE = "issue-fix:handback"

# A rejection's detail leads with the reviewer: "design (opus): …".
_REVIEWER_RE = re.compile(r"^([a-z-]+(?: \([^)]*\))?): ")

_JUDGED_REPRO = ("not-reproduced", "unwritable", "wrong-symptom")

# A fix-unproven detail's leading words name the check it failed.
_UNPROVEN = (("compile lane", "compile"), ("lint", "compile"),
             ("full suite", "suite"), ("related-tests", "suite"),
             ("preservation tests", "suite"),
             ("independent reproduction", "reproduction"),
             ("only one reading", "readings"), ("chosen reading", "readings"),
             ("passes its own reproduction", "no-fix"))

# Endings on the report itself: it is not a defect, its readings disagree, the
# intake audit refused it, or it closed or changed mid-run.
_SUBJECT = {"not-a-defect": "not-a-defect", "fix-disputed": "readings",
            "refused": "declined", "cancelled": "declined"}


def _stage(ending: str, fault: bool, detail: str) -> tuple[str, Fault | None]:
    """(stage, fault) for a run that ended `ending` with `detail`."""
    if ending in ("fixed", "reproduced"):
        return ending, None
    if fault or ending in ("sandbox", "agent-unavailable", "run-failed", "base-compile"):
        return {"sandbox": "sandbox", "base-compile": "base"}.get(ending, "agent"), "machine"
    if ending in _SUBJECT:
        return _SUBJECT[ending], "subject"
    if ending in _JUDGED_REPRO:
        return "reproduction", "judged"
    if ending == "fix-unproven":
        for marker, stage in _UNPROVEN:
            if marker in detail:
                return stage, "judged"
        return "proof", "judged"
    if ending == "fix-pinned":
        return "suite", "judged"
    if ending == "fix-rejected":
        return ("revision" if detail.startswith("the revision") else "review"), "judged"
    if ending == "fix-untrusted":
        return "guardrail", "judged"
    if ending == "no-fix":
        return "no-fix", "judged"
    return "declined", "judged"


def _asked(trigger: str, extra: dict[str, str] | None) -> Asked | None:
    words = (extra or {}).get("guidance") or (extra or {}).get("notes")
    if not words:
        return None
    return {"by": str((extra or {}).get("requested_by") or trigger), "words": str(words)}


def of_run(issue: int, report_sha: str | None, *, ending: str, fault: bool, detail: str,
           trigger: str, extra: dict[str, str] | None = None) -> Outcome:
    stage, blame = _stage(ending, fault, detail)
    by = None
    if stage == "review" and (m := _REVIEWER_RE.match(detail)):
        by = m.group(1)
    return outcomes.make("issue-fix", {"kind": "issue", "number": issue, "sha": report_sha},
                         stage, fault=blame, by=by, words=detail, asked=_asked(trigger, extra))


def leaves(result: dict | None) -> list[str]:
    """The reported defects a fixed run's change does not address."""
    res = result or {}
    coverage = issue_gates.fix_coverage(issue_gates.latest_reviews(res.get("reviews")),
                                        res.get("claims"))
    if coverage.state != "partial":
        return []
    return [d["defect"] for d in coverage.having("not-addressed")]


def _handback_stage(by: str | None, outside: bool) -> str:
    if by == CI:
        return "ci-outside" if outside else "ci"
    if by is None:
        return "fixer"
    return "maintainer" if by == MAINTAINER else "reviewer"


def of_handback(issue: int, pr: int, head: str | None, *, by: str | None, reason: str,
                outside: bool, words: str = "") -> Outcome:
    """A follow-up hand-back: `by` names whose bar the change could not clear,
    or is None when the fixer itself could not make the change asked for."""
    stage = _handback_stage(by, outside)
    return outcomes.make("issue-followup", {"kind": "pr", "number": pr, "sha": head}, stage,
                         fault="subject" if outside else "judged", by=by,
                         words=f"{reason}\n\n{words}".strip())


def handback_row(issue: int, pr: int, head: str | None, *, by: str | None, reason: str,
                 outside: bool, revisions: int, words: str = "") -> dict[str, object]:
    """The ledger row for the follow-up handing issue's pull request back."""
    now = storekit.now()
    return {"phase": HANDBACK_PHASE, "issue": issue, "pr": pr, "started": now,
            "finished": now, "trigger": "followup", "stats": {"revisions": revisions},
            "outcome": of_handback(issue, pr, head, by=by, reason=reason, outside=outside,
                                   words=words)}


def of_row(raw: dict) -> Outcome | None:
    """The outcome a ledger row carries, or for an `issue-fix:run` row written
    before outcomes, the one its ending and detail give."""
    got = outcomes.read(raw.get("outcome"))
    if got is not None or raw.get("phase") != RUN_PHASE:
        return got
    stats = raw.get("stats") or {}
    if not isinstance(raw.get("issue"), int) or not stats.get("ending"):
        return None
    return of_run(raw["issue"], stats.get("report_sha"), ending=str(stats["ending"]),
                  fault=bool(stats.get("fault")), detail=str(stats.get("detail") or ""),
                  trigger=str(raw.get("trigger") or ""))
