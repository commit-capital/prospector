"""Ask a disputed issue its question, and resume the run once it is answered.

`python -m issue_triage.question --issue N --ask [--live]` asks through
`executor.ask_issue_question` (a dry-run unless --live).
`python -m issue_triage.question --issue N --resume` reads the replies
(`dispute_question.read_answer`); with an answer, or with none once the wait
has passed (the question's default), it resumes the disputed run on the chosen
reading (`cross_lane.judge_reading`) over the base the dispute was proven on.
The new ending replaces the issue's `result.json`, the disputed one is kept
beside it as `disputed.json`, and one `issue-fix:run` row is appended to the
issue runs ledger. A `fixed` ending is then proposed like any other.
"""
from __future__ import annotations

import argparse
import json
import sys

from issue_triage import cross_lane, dispute_question, fetch_issues, fix_lane, propose
from issue_triage.issue_store import IssueStore
from pipeline import prove, settings, storekit


def resume(issue: int) -> int:
    record = propose.load_result(issue)
    qpath = propose.result_dir(issue) / "question.json"
    saved = json.loads(qpath.read_text()) if qpath.exists() else {}
    posted = saved.get("posted")
    if record is None or record.get("ending") != "fix-disputed" or not posted:
        print(f"issue #{issue} has no asked question to resume", file=sys.stderr)
        return 2
    live = fetch_issues.fetch_issue(issue)
    if live is None or live.get("state") != "open":
        print(f"issue #{issue} is closed or cannot be read; nothing to resume")
        return 0
    if fix_lane.report_sha(live["title"], live.get("body") or "") != record["report_sha"]:
        print(f"issue #{issue}'s report was edited after the run; re-run the lane on it")
        return 0

    question = saved["question"]
    options = [o["label"] for o in question["options"]]
    answer = dispute_question.read_answer(issue, asked_at=posted["asked_at"], options=options,
                                          issue_author=live.get("author") or "")
    if answer is None:
        if not dispute_question.default_due(posted["asked_at"]):
            print(f"issue #{issue}: no answer yet")
            return 0
        answer = {"label": question["default"], "default": True}
    reading = record["result"]["readings"][options.index(answer["label"])]

    try:
        base = prove.held(record["base_sha"], int(record.get("base_tier") or 0))
    except prove.NoBase as e:
        print(str(e), file=sys.stderr)
        return 2
    spec = fix_lane.LaneSpec(issue=issue, title=live["title"], body=live.get("body") or "",
                             base=base)
    workdir = propose.result_dir(issue)
    started = storekit.now()
    res = cross_lane.judge_reading(spec, workdir=workdir, result=record["result"],
                                   reading=reading, on_step=lambda s: print(s, flush=True))
    finished = storekit.now()

    disputed = workdir / "disputed.json"
    if not disputed.exists():
        disputed.write_text(json.dumps(record, indent=2) + "\n")
    (workdir / "result.json").write_text(json.dumps({
        **record, "ending": res.ending, "fault": res.fault, "detail": res.detail,
        "agent_runs": res.agent_runs, "started": started, "finished": finished,
        "reproduction": res.reproduction, "result": res.result, "answer": answer,
    }, indent=2) + "\n")
    saved["answered"] = {**answer, "ending": res.ending}
    qpath.write_text(dispute_question.dumps(saved))
    IssueStore().append_run({
        "phase": "issue-fix:run", "issue": issue, "started": started, "finished": finished,
        "trigger": "answer",
        "stats": {"action": "fix", "lane": "cross", "ending": res.ending, "fault": res.fault,
                  "detail": res.detail, "host": settings.worker_id(),
                  "base_sha": base.sha, "report_sha": record["report_sha"],
                  "agent_runs": res.agent_runs, "answer": answer["label"],
                  "answer_default": bool(answer.get("default"))},
    })
    print(f"{res.ending}: {res.detail}")
    return 1 if res.fault else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m issue_triage.question",
        description="Ask a disputed issue the question its run raised, or resume the run "
                    "on the answer.")
    ap.add_argument("--issue", type=int, required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ask", action="store_true", help="draft and ask the question")
    mode.add_argument("--resume", action="store_true",
                      help="read the answer and resume the run on it")
    ap.add_argument("--live", action="store_true", help="with --ask: post it as the bot")
    args = ap.parse_args(argv)
    if args.resume:
        return resume(args.issue)
    from prospector_app.backend import executor

    token = executor.mint_bot_token() if args.live else None
    res = executor.ask_issue_question(args.issue, token=token, dry_run=not args.live)
    print(json.dumps(res, indent=2))
    return 0 if res["status"] in ("executed", "dry-run", "exists") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
