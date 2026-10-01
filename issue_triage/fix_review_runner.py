"""Carry out one issue-fix request (`fix_review.queue`) on the machine that holds
the sandbox, and write what happened back to the issue.

- `solve` runs the cross-tested lane with the request's guidance; a dispute
  drafts its question at once (`dispute_question.draft`, kept beside the result
  so a later ask posts the same words).
- `send-back` asks a small agent whether the comments keep the fix's approach
  (`revise`: one agent revises the current change on a clone that holds it, then
  the host's checks and the scope review) or discard it (`restart`: `solve` with
  the comments as guidance).
- `answer` resumes a dispute on the chosen reading (`cross_lane.judge_reading`),
  or re-solves with the question and a written answer as guidance.
- `ask-reporter` and `propose` take the executor's bot paths.

Each run writes `result.json` and its ledger row (`fix_lane.record_result`),
then the distilled `fix_run`; the request ends `done` with the outcome, or
`failed` with the reason, and the worker's note joins the thread.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone

from issue_triage import (
    cross_lane,
    dispute_question,
    fix_lane,
    fix_review,
    propose,
    solo_lane,
)
from issue_triage.issue_store import IssueStore
from pipeline import headless_agent, prove, settings, storekit
from pipeline.store import Store

ROUTE_PROMPT = """\
A maintainer reviewing an automated fix for a bug report wrote the comments below. Decide what they ask for: to keep the fix's approach and adjust it ("revise"), or to discard it and solve the bug again another way ("restart").

<comments>
__COMMENTS__
</comments>

Return ONLY a JSON object, as a ```json fenced block: {"mode": "revise" | "restart"}
"""


class RequestFailed(RuntimeError):
    """The request could not be carried out; the message says why."""


def route(comments: str) -> str:
    """`revise` or `restart` for a maintainer's comments on a fix; `revise`
    when the agent gives no usable answer."""
    prompt = headless_agent.fill(ROUTE_PROMPT, {"__COMMENTS__": comments.strip()})
    try:
        with headless_agent.workdir("prospector-route-") as tmp:
            verdict, _ = headless_agent.json_reply(lambda: headless_agent.run_agent(
                prompt, allow_gh=False, cwd=tmp, read_root=tmp, env_allow=(), timeout=300))
    except (headless_agent.AgentUnavailable, headless_agent.AgentDeclined):
        raise
    except (RuntimeError, ValueError):
        return "revise"
    return "restart" if verdict.get("mode") == "restart" else "revise"


def _spec(store: IssueStore, n: int, base: prove.PinnedBase,
          guidance: str | None) -> tuple[fix_lane.LaneSpec, str]:
    reported = fix_lane.reported_text(store, n)
    if reported is None:
        raise RequestFailed(f"issue #{n} is not in the store and could not be fetched")
    title, body = reported
    return (fix_lane.LaneSpec(issue=n, title=title, body=body, base=base, guidance=guidance),
            fix_lane.report_sha(title, body))


def _held_base(record: dict) -> prove.PinnedBase:
    try:
        return prove.held(record["base_sha"], int(record.get("base_tier") or 0))
    except prove.NoBase as e:
        raise RequestFailed(f"the base this attempt was proven on is gone: {e}") from e


def _revision_base(record: dict) -> prove.PinnedBase:
    """The base a revision is proven on: the attempt's own when this machine
    still holds it, else the current pin."""
    try:
        return prove.held(record["base_sha"], int(record.get("base_tier") or 0))
    except prove.NoBase:
        try:
            return prove.pinned(Store())
        except prove.NoBase as e:
            raise RequestFailed(f"no base to prove a revision on: {e}") from e


def _last_record(n: int) -> dict:
    record = propose.load_result(n)
    if record is None:
        raise RequestFailed(f"this machine holds no result for issue #{n}")
    return record


def _keep_question(n: int, report: str, question: dict) -> None:
    (propose.result_dir(n) / "question.json").write_text(
        dispute_question.dumps({"report_sha": report, "question": question}))


def solve(store: IssueStore, n: int, *, guidance: str | None, trigger: str,
          on_step: Callable[[str], None]) -> tuple[dict, dict | None]:
    """The cross-tested lane on issue `n`; its record and, for a dispute, the
    drafted question."""
    try:
        base = prove.pinned(Store())
    except prove.NoBase as e:
        raise RequestFailed(str(e)) from e
    spec, report = _spec(store, n, base, guidance)
    workdir = propose.result_dir(n)
    started = storekit.now()
    res = cross_lane.run(spec, workdir=workdir, on_step=on_step)
    if res.ending == "fixed":
        reason = fix_lane.still_valid_for(n, report)
        if reason:
            res = fix_lane.LaneResult(ending="cancelled", fault=False, detail=reason,
                                      reproduction=res.reproduction, result=res.result,
                                      agent_runs=res.agent_runs)
    record = fix_lane.record_result(
        store, workdir, issue=n, report=report, base=base, action="fix", lane="cross",
        models=list(settings.issue_fix_models()), res=res, started=started,
        finished=storekit.now(), trigger=trigger,
        extra={"guidance": guidance} if guidance else None)
    question = None
    if res.ending == "fix-disputed" and len((res.result or {}).get("readings") or []) >= 2:
        on_step("drafting the question")
        try:
            question = dispute_question.draft(spec.title, spec.body, res.result or {})
        except (RuntimeError, ValueError) as e:
            if isinstance(e, (headless_agent.AgentUnavailable, headless_agent.AgentDeclined)):
                raise
            question = {"no_question": f"drafting the question failed: {e}"}
        _keep_question(n, report, question)
    return record, question


def revise(store: IssueStore, n: int, *, comments: str,
           on_step: Callable[[str], None], trigger: str = "send-back") -> dict:
    """One agent revises the last attempt's change under `comments`, proven on
    the attempt's base or, when this machine no longer holds it, the current
    pin."""
    previous = _last_record(n)
    res_prev = previous.get("result") or {}
    if not res_prev.get("patch"):
        raise RequestFailed("the last attempt holds no change to revise")
    base = _revision_base(previous)
    spec, report = _spec(store, n, base, comments)
    reviews = res_prev.get("reviews") or []
    review = "; ".join(filter(None, [str(r.get("reason") or "") for r in reviews]
                              + [str(c) for r in reviews for c in r.get("concerns") or []]))
    workdir = propose.result_dir(n)
    started = storekit.now()
    res = solo_lane.run(spec, workdir=workdir, on_step=on_step, start_patch=res_prev["patch"],
                        attempt={"summary": res_prev.get("summary"),
                                 "root_cause": res_prev.get("root_cause"),
                                 "review": review or None},
                        review=True)
    return fix_lane.record_result(
        store, workdir, issue=n, report=report, base=base, action="fix", lane="revise",
        models=[settings.agent_model() or "default"], res=res, started=started,
        finished=storekit.now(), trigger=trigger, extra={"guidance": comments})


def answer(store: IssueStore, n: int, answer_: dict, *,
           on_step: Callable[[str], None]) -> tuple[dict, dict | None]:
    """Resume a dispute on the chosen reading, or re-solve with a written
    answer."""
    previous = _last_record(n)
    qpath = propose.result_dir(n) / "question.json"
    saved = json.loads(qpath.read_text()) if qpath.exists() else {}
    question = saved.get("question") or {}
    options = [o["label"] for o in question.get("options") or []]
    label = answer_.get("label")
    if label and label in options:
        readings = (previous.get("result") or {}).get("readings") or []
        reading = readings[options.index(label)]
        base = _held_base(previous)
        spec, report = _spec(store, n, base, None)
        workdir = propose.result_dir(n)
        started = storekit.now()
        res = cross_lane.judge_reading(spec, workdir=workdir, result=previous["result"],
                                       reading=reading, on_step=on_step)
        record = fix_lane.record_result(
            store, workdir, issue=n, report=report, base=base, action="fix", lane="cross",
            models=previous.get("models") or [], res=res, started=started,
            finished=storekit.now(), trigger="answer", extra={"answer": answer_})
        return record, {**question, "answered": answer_}
    text = str(answer_.get("text") or "").strip()
    guidance = (f"The fix attempt asked: {question.get('question') or '(no question)'}\n"
                f"The maintainer answered: {text}")
    return solve(store, n, guidance=guidance, trigger="answer", on_step=on_step)


def _write_run(store: IssueStore, n: int, record: dict, question: dict | None) -> dict:
    run = fix_review.distill(record, question)
    store.edit_issue(n).record_fix_run(run)
    return run


def _note(store: IssueStore, n: int, text: str) -> None:
    store.edit_issue(n).append_fix_thread({"at": storekit.now(), "by": "worker",
                                           "kind": "note", "text": text})


def run_request(store: IssueStore, n: int, req: dict, *,
                on_step: Callable[[str], None] = lambda step: None) -> tuple[str, str]:
    """Carry out issue `n`'s claimed request `req`, recording the outcome on the
    issue. Returns the request's ending status (`done`, `failed`) and the
    one-line outcome. An agent outage propagates, after the request is marked
    failed."""
    action = req["action"]
    try:
        outcome = _carry_out(store, n, action, req, on_step=on_step)
        status = "done"
    except headless_agent.AgentUnavailable as e:
        _finish(store, n, req, "failed", f"the agent is unavailable: {e}")
        raise
    except (RequestFailed, prove.NoBase, prove.SuiteFault, RuntimeError, ValueError) as e:
        outcome, status = f"{action} failed: {e}", "failed"
    _finish(store, n, req, status, outcome)
    _note(store, n, outcome)
    return status, outcome


def _finish(store: IssueStore, n: int, req: dict, status: str, reason: str) -> None:
    """End the claim `req` as `status` while it is still issue `n`'s request,
    running or taken back by orphan recovery; a request queued since is left
    for its own run."""
    issue = store.edit_issue(n)
    held = issue.fix_request or {}
    if (held.get("host"), held.get("started_at")) != (req.get("host"), req.get("started_at")):
        return
    issue.record_fix_request({**req, "status": status, "finished_at": storekit.now(),
                              "reason": reason[:500]})


def _carry_out(store: IssueStore, n: int, action: str, req: dict, *,
               on_step: Callable[[str], None]) -> str:
    guidance = req.get("guidance")
    trigger = "hunter" if req.get("source") == "hunter" else "operator"
    if action == "solve":
        record, question = solve(store, n, guidance=guidance, trigger=trigger, on_step=on_step)
        _write_run(store, n, record, question)
        return f"Solved: {record['ending']} — {record['detail']}"
    if action == "send-back" and req.get("source") == "followup":
        return _follow_up(store, n, guidance or "", on_step=on_step)
    if action == "send-back":
        mode = route(guidance or "")
        on_step(f"the comments ask to {mode}")
        if mode == "restart":
            record, question = solve(store, n, guidance=guidance, trigger="send-back",
                                     on_step=on_step)
            _write_run(store, n, record, question)
            return f"Started over: {record['ending']} — {record['detail']}"
        record = revise(store, n, comments=guidance or "", on_step=on_step)
        _write_run(store, n, record, None)
        return f"Revised: {record['ending']} — {record['detail']}"
    if action == "answer":
        record, question = answer(store, n, req.get("answer") or {}, on_step=on_step)
        _write_run(store, n, record, question)
        return f"Resumed on the answer: {record['ending']} — {record['detail']}"
    if action in ("ask-reporter", "propose"):
        return _bot_action(store, n, action, dry_run=bool(req.get("dry_run")))
    raise RequestFailed(f"unknown action {action!r}")


def _follow_up(store: IssueStore, n: int, guidance: str, *,
               on_step: Callable[[str], None]) -> str:
    """Revise the fix behind issue `n`'s open pull request under the follow-up's
    `guidance` and, when the revision ends `fixed`, push it onto the same
    branch (`executor.update_issue_fix_proposal`). A revision that ends any
    other way leaves the pull request, the run it shows, and the result on
    disk as they were."""
    from prospector_app.backend import executor

    issue = store.load_issue(n)
    run = dict((issue.fix_run if issue else None) or {})
    proposal = run.get("proposal") or {}
    pr = proposal.get("pr")
    if not pr:
        raise RequestFailed("the attempt has no open pull request to follow up")
    result_file = propose.result_dir(n) / "result.json"
    kept = result_file.read_text()
    try:
        record = revise(store, n, comments=guidance, on_step=on_step, trigger="followup")
    except BaseException:
        result_file.write_text(kept)
        raise
    if record.get("ending") != "fixed":
        result_file.write_text(kept)
        return (f"The revision ended {record.get('ending')}: {record.get('detail')}; "
                f"#{pr} is unchanged")
    on_step("pushing the revision")
    live = settings.issue_fix_followup() == "live"
    token = executor.mint_bot_token() if live else None
    res = executor.update_issue_fix_proposal(n, int(pr), push=True, token=token,
                                             dry_run=not live)
    if res.get("status") in ("blocked", "error"):
        result_file.write_text(kept)
        raise RequestFailed(f"the revision could not go onto #{pr}: {res.get('detail')}")
    if res.get("status") == "dry-run":
        result_file.write_text(kept)
        return f"Dry run: {res.get('detail')}"
    distilled = fix_review.distill(record)
    distilled["proposal"] = proposal
    store.edit_issue(n).record_fix_run(distilled)
    return f"Revised #{pr}: {res.get('detail')}"


def _bot_action(store: IssueStore, n: int, action: str, *, dry_run: bool) -> str:
    from prospector_app.backend import executor

    token = None if dry_run else executor.mint_bot_token()
    if action == "ask-reporter":
        res = executor.ask_issue_question(n, token=token, dry_run=dry_run)
    else:
        res = executor.propose_issue_fix(n, token=token, dry_run=dry_run)
    status = res.get("status")
    if status in ("blocked", "error"):
        raise RequestFailed(str(res.get("detail")))
    issue = store.edit_issue(n)
    run = dict(issue.fix_run or {})
    if action == "ask-reporter" and status == "executed":
        run["question"] = {**(run.get("question") or {}),
                           "asked": {"url": res.get("url"),
                                     "at": datetime.now(timezone.utc).isoformat(
                                         timespec="seconds")}}
        issue.record_fix_run(run)
    if action == "propose" and status in ("executed", "exists"):
        pr = res.get("pr")
        if pr is None and res.get("url"):
            pr = str(res["url"]).rstrip("/").rsplit("/", 1)[-1]
        run["proposal"] = {"pr": pr, "url": res.get("url")}
        issue.record_fix_run(run)
    prefix = "Dry run: " if status == "dry-run" else ""
    return f"{prefix}{res.get('detail')}"
