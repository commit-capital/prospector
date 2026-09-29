"""Record the issue-fix results this machine already holds on their issues, so
attempts made from the command line show in the app.

`python -m issue_triage.fix_review_backfill [--draft-questions] [--live]` reads
every `<verify scratch>/issue-fix/issue-<n>/result.json` and, for an issue whose
store record has no `fix_run` yet, writes the distilled run
(`fix_review.distill`) with the question kept beside the result. A proposal the
Activity log records as opened becomes the run's `proposal`. With
`--draft-questions` a disputed run with no question drafts one
(`dispute_question.draft`, one agent run each). A dry-run unless `--live`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from issue_triage import dispute_question, fix_lane, fix_review, propose
from issue_triage.issue_store import IssueStore
from pipeline import settings


def _proposal(n: int) -> dict | None:
    from prospector_app.backend import activity

    for ev in activity.for_issue(n):
        if ev.get("kind") != "issue-propose" or ev.get("status") not in ("executed", "exists"):
            continue
        pr = ev.get("pr")
        if pr is None:
            m = re.search(r"#(\d+)", str(ev.get("detail") or ""))
            pr = int(m.group(1)) if m else None
        if pr is not None:
            return {"pr": pr, "url": ev.get("url")
                    or f"https://github.com/{settings.repo()}/pull/{pr}"}
    return None


def backfill(store: IssueStore, *, draft_questions: bool, live: bool) -> list[str]:
    root = settings.verify_scratch() / "issue-fix"
    out: list[str] = []
    for d in sorted(root.glob("issue-*")):
        try:
            n = int(d.name.split("-", 1)[1])
            record = json.loads((d / "result.json").read_text())
        except (ValueError, OSError):
            continue
        issue = store.load_issue(n)
        if issue is None or issue.fix_run:
            continue
        qpath = d / "question.json"
        saved = json.loads(qpath.read_text()) if qpath.exists() else {}
        question = saved.get("question")
        readings = (record.get("result") or {}).get("readings") or []
        if (question is None and draft_questions and record.get("ending") == "fix-disputed"
                and len(readings) >= 2):
            reported = fix_lane.reported_text(store, n)
            if reported and fix_lane.report_sha(*reported) == record.get("report_sha"):
                question = dispute_question.draft(reported[0], reported[1], record["result"])
                if live:
                    (propose.result_dir(n) / "question.json").write_text(dispute_question.dumps(
                        {"report_sha": record["report_sha"], "question": question}))
        run = fix_review.distill(record, question)
        run["proposal"] = _proposal(n)
        out.append(f"#{n}: {record.get('ending')}"
                   + (" with a question" if run.get("question") else "")
                   + (f", PR {run['proposal']['pr']}" if run["proposal"] else ""))
        if live:
            store.edit_issue(n).record_fix_run(run)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m issue_triage.fix_review_backfill",
                                 description=__doc__)
    ap.add_argument("--draft-questions", action="store_true")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args(argv)
    for line in backfill(IssueStore(), draft_questions=args.draft_questions, live=args.live):
        print(("" if args.live else "would record ") + line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
