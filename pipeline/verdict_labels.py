"""The ONE policy for what a label on a reviewer's recorded run says.

A label is the verdict the reviewer should have given (`expected`, one of its
judging verdicts in `verdict_log.VERDICTS`), optional marks on its findings by
index, a note, and who set it from where. A run that never judged (its
reviewer's failure value) takes no label. `call` reads a label against the
run's verdict as right, an under-call or an over-call, by severity order.

`backfill_issue` records an issue's fix attempt that ran before capture from
its stored `fix_run`, so it can be labeled. Its prompt is unknown
(`prompt_sha` "backfill") and the evidence the reviewers read was never kept
(`partial_inputs`).

Usage:
  uv run python -m pipeline.verdict_labels backfill --issue N
"""
from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Literal

from pipeline import storekit, verdict_log
from pipeline.store import Store

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue

MARKS: tuple[str, ...] = ("real", "wrong", "intentional", "didnt-matter")
# The fewest labeled cases on a side before a rate is read, or a gate refuses.
MIN_CASES = 10
# The least severe verdict that blocks, per reviewer.
BLOCKING: dict[str, str] = {"design": "fail", "security": "RED", "fix-review": "unsafe",
                            "trust-boundary": "hold", "intake": "suspicious",
                            "fixability": "skip"}
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
    indexes = {str(i) for i in range(count)}
    for key, mark in marks.items():
        if key not in indexes:
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


def blocks(reviewer: str, verdict: str) -> bool:
    order = verdict_log.VERDICTS[reviewer]
    return order.index(verdict) >= order.index(BLOCKING[reviewer])


def call(label: dict, verdict: str, reviewer: str) -> Literal["right", "under", "over"]:
    order = verdict_log.VERDICTS[reviewer]
    given, wanted = order.index(verdict), order.index(label["expected"])
    return "right" if given == wanted else "under" if given < wanted else "over"


def backfill_issue(store: Store, issue: Issue) -> list[int]:
    """Record `issue`'s stored fix attempt as fix-review, trust-boundary and
    intake runs, skipping any already backfilled from the same attempt; the new
    rows' ids."""
    from issue_triage import intake_audit, reproduce_issue, review_issue_fix, trust_boundary
    run = issue.fix_run
    if not run:
        return []
    started = str(run.get("started") or "")
    # A reviewer already has this attempt when a backfill took it, or when live
    # capture recorded a run at or after the attempt started.
    done = {r.reviewer for r in store.review_verdicts(subject_kind="issue", subject=issue.number)
            if (r.data.get("backfill") or {}).get("fix_run_started") == started
            or ("backfill" not in r.data and r.at >= started)}
    meta = issue.raw.get("meta") or {}
    inputs = {"report": reproduce_issue.report_block(str(meta.get("title") or ""),
                                                     str(meta.get("body") or "")),
              "root_cause": str(run.get("root_cause") or ""),
              "patch": str(run.get("patch") or ""), "evidence": ""}
    found: list[tuple[str, str, dict, str]] = []
    if run.get("reviews"):
        models = ",".join(sorted({str(r.get("model")) for r in run["reviews"]}))
        found.append(("fix-review", review_issue_fix.verdict_of(run["reviews"]),
                      {"reviews": run["reviews"]}, models))
    if run.get("boundary"):
        found.append(("trust-boundary", trust_boundary.verdict_of(run["boundary"]),
                      run["boundary"], "unknown"))
    if run.get("intake"):
        found.append(("intake", intake_audit.verdict_of(run["intake"]), run["intake"], "unknown"))
    ids = []
    for reviewer, verdict, data, model in found:
        if reviewer in done:
            continue
        vid = verdict_log.record(
            reviewer, verdict_log.Subject("issue", issue.number), verdict=verdict,
            prompt_sha="backfill", model=model,
            data={**data, "backfill": {"fix_run_started": started}, "partial_inputs": True,
                  "patch_truncated": bool(run.get("patch_truncated"))},
            inputs=inputs, base_sha=run.get("base_sha"), store=store)
        if vid is not None:
            ids.append(vid)
    return ids


def main(argv: list[str]) -> int:
    from issue_triage.issue_store import IssueStore
    ap = argparse.ArgumentParser(prog="python -m pipeline.verdict_labels")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backfill").add_argument("--issue", type=int, required=True)
    args = ap.parse_args(argv)
    issue = IssueStore().load_issue(args.issue)
    if issue is None:
        print(f"issue #{args.issue} is not in the store", file=sys.stderr)
        return 2
    ids = backfill_issue(Store(), issue)
    print(f"issue #{args.issue}: {len(ids)} run(s) recorded" + (f": {ids}" if ids else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
