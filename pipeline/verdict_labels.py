"""The ONE policy for what a label on a reviewer's recorded run says.

A label is the verdict the reviewer should have given (`expected`, one of its
judging verdicts in `verdict_log.VERDICTS`), optional marks on its findings by
index, a note, and who set it from where. A run that never judged (its
reviewer's failure value) takes no label. `call` reads a label against the
run's verdict as right, an under-call or an over-call, by severity order.
"""
from __future__ import annotations

from typing import Literal

from pipeline import storekit, verdict_log
from pipeline.store import Store

MARKS: tuple[str, ...] = ("real", "wrong", "intentional", "didnt-matter")
SOURCES: tuple[str, ...] = ("operator", "case-study")
NOTE_MAX = 4000


class LabelRefused(ValueError):
    pass


def judging(reviewer: str) -> tuple[str, ...]:
    return verdict_log.VERDICTS[reviewer][:-1]


def _findings(data: dict) -> list:
    found = data.get("confirmed") if "confirmed" in data else data.get("findings")
    return found if isinstance(found, list) else []


def label(store: Store, verdict_id: int, *, expected: str, by: str, note: str = "",
          findings: dict[str, str] | None = None, source: str = "operator") -> dict:
    """Set and return run `verdict_id`'s label. KeyError when there is no such run."""
    row = store.review_verdict(verdict_id)
    if row is None:
        raise KeyError(verdict_id)
    if row.verdict not in judging(row.reviewer):
        raise LabelRefused(f"this {row.reviewer} run never judged ({row.verdict}), "
                           "so it takes no label")
    if expected not in judging(row.reviewer):
        raise LabelRefused(f"{expected!r} is not a {row.reviewer} verdict; "
                           f"one of {', '.join(judging(row.reviewer))}")
    marks = findings or {}
    count = len(_findings(row.data))
    for key, mark in marks.items():
        if not (key.isdecimal() and int(key) < count):
            raise LabelRefused(f"finding {key!r} is not one of this run's {count} findings")
        if mark not in MARKS:
            raise LabelRefused(f"{mark!r} is not a finding mark; one of {', '.join(MARKS)}")
    if source not in SOURCES:
        raise LabelRefused(f"{source!r} is not a label source")
    if not by.strip():
        raise LabelRefused("a label needs the operator who set it")
    out = {"expected": expected, "findings": marks, "note": note.strip()[:NOTE_MAX],
           "by": by.strip(), "at": storekit.now(), "source": source}
    store.set_review_verdict_label(verdict_id, out)
    return out


def call(label: dict, verdict: str, reviewer: str) -> Literal["right", "under", "over"]:
    order = verdict_log.VERDICTS[reviewer]
    given, wanted = order.index(verdict), order.index(label["expected"])
    return "right" if given == wanted else "under" if given < wanted else "over"
