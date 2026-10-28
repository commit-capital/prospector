"""The ONE shape of what a lane's attempt came to, carried as `outcome` on its
runs-ledger row, so every lane's results read, count and compare the same way.

An outcome names its `lane`, the `subject` it worked on (a PR or an issue, at
the head or report SHA it saw), the `stage` it stopped at from that lane's
fixed list (STAGES), whose `fault` the stop was, who said so (`by`), and their
own `words`. `asked` carries who asked for the attempt and their words, when
someone did.

Faults: `machine` is Prospector or its infrastructure (a sandbox that broke, an
agent outage, a tool error); `subject` is the PR or report itself (a report
that is not a defect, CI that fails outside the change); `judged` is a verdict
on the work. A success has no fault.
"""
from __future__ import annotations

from typing import Literal, TypedDict

FAULTS = ("machine", "subject", "judged")
WORDS_MAX = 1200

# Each lane's stages, success first.
STAGES: dict[str, tuple[str, ...]] = {
    "issue-fix": ("fixed", "reproduced", "reproduction", "readings", "no-fix",
                  "not-a-defect", "compile", "suite", "review", "revision", "guardrail",
                  "declined", "sandbox", "agent", "base"),
    "issue-followup": ("ci", "ci-outside", "reviewer", "maintainer", "fixer"),
}


class Subject(TypedDict):
    kind: Literal["pr", "issue"]
    number: int
    sha: str | None


class Asked(TypedDict):
    by: str
    words: str


class Outcome(TypedDict):
    lane: str
    subject: Subject
    stage: str
    fault: str | None
    by: str | None
    words: str
    asked: Asked | None


def make(lane: str, subject: Subject, stage: str, *, fault: str | None,
         by: str | None = None, words: str = "", asked: Asked | None = None) -> Outcome:
    if stage not in STAGES.get(lane, ()):
        raise ValueError(f"{lane!r} has no stage {stage!r}")
    if fault is not None and fault not in FAULTS:
        raise ValueError(f"unknown fault {fault!r}")
    if asked is not None:
        asked = {"by": asked["by"], "words": asked["words"][:WORDS_MAX]}
    return {"lane": lane, "subject": subject, "stage": stage, "fault": fault, "by": by,
            "words": words[:WORDS_MAX], "asked": asked}


def read(raw: object) -> Outcome | None:
    """The outcome a ledger row carries, or None when it has none or names a
    stage its lane does not have."""
    if not isinstance(raw, dict):
        return None
    lane, stage = raw.get("lane"), raw.get("stage")
    subject = raw.get("subject")
    if not (isinstance(lane, str) and stage in STAGES.get(lane, ())
            and isinstance(subject, dict) and isinstance(subject.get("number"), int)):
        return None
    fault = raw.get("fault")
    asked = raw.get("asked")
    return {"lane": lane,
            "subject": {"kind": "pr" if subject.get("kind") == "pr" else "issue",
                        "number": subject["number"],
                        "sha": subject["sha"] if isinstance(subject.get("sha"), str) else None},
            "stage": str(stage), "fault": fault if fault in FAULTS else None,
            "by": raw["by"] if isinstance(raw.get("by"), str) else None,
            "words": str(raw.get("words") or ""),
            "asked": ({"by": str(asked.get("by") or ""), "words": str(asked.get("words") or "")}
                      if isinstance(asked, dict) else None)}
