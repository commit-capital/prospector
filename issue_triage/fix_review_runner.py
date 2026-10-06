"""Carry out one issue-fix request (`fix_review.queue`) on the machine that holds
the sandbox, and write what happened back to the issue.

- `solve` runs the cross-tested lane with the request's guidance and notes
  (a maintainer's instruction, and anyone else's words as data), first having
  `intake_audit` judge an outsider's report: an unattended solve of a report it
  reads as malicious ends `refused` with no lane run; a dispute
  drafts its question at once (`dispute_question.draft`, kept beside the result
  so a later ask posts the same words).
- `send-back` asks a small agent whether the comments keep the fix's approach
  (`revise`: one agent revises the current change on a clone that holds it, then
  the host's checks and the scope review) or discard it (`restart`: `solve` with
  the comments as guidance). While the attempt is open as a pull request
  (`fix_review.open_pr`), a fixed revision goes onto it as one more commit, the
  way the follow-up's own revisions do, and a restart is refused: the pull
  request and its reviews belong to the approach the comments discard.
- `answer` resumes a dispute on the chosen reading (`cross_lane.judge_reading`),
  or re-solves with the question and a written answer: a maintainer's as
  guidance, the issue author's as notes.
- `ask-reporter` and `propose` take the executor's bot paths.

Each run writes `result.json` and its ledger row (`fix_lane.record_result`),
then the distilled `fix_run`; the request ends `done` with the outcome, or
`failed` with the reason, and the worker's note joins the thread. Before and
after every request the runner checks whether someone else's pull request took
the issue up (`superseded.check`): before, a request on such an issue ends
`cancelled` without running; after, the attempt it recorded is marked.
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
    intake_audit,
    propose,
    solo_lane,
    superseded,
)
from issue_triage.issue_store import IssueStore
from pipeline import gates, headless_agent, prove, settings, storekit, threats
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


def _spec(store: IssueStore, n: int, base: prove.PinnedBase, guidance: str | None,
          notes: str | None = None) -> tuple[fix_lane.LaneSpec, str]:
    reported = fix_lane.reported_text(store, n)
    if reported is None:
        raise RequestFailed(f"issue #{n} is not in the store and could not be fetched")
    title, body = reported
    return (fix_lane.LaneSpec(issue=n, title=title, body=body, base=base, guidance=guidance,
                              notes=notes),
            fix_lane.report_sha(title, body))


def _words(guidance: str | None, notes: str | None) -> dict[str, str] | None:
    """What a run record keeps of the words the request carried."""
    return {k: v for k, v in (("guidance", guidance), ("notes", notes)) if v} or None


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


def _intake(store: IssueStore, n: int, spec: fix_lane.LaneSpec,
            on_step: Callable[[str], None]) -> dict | None:
    """`intake_audit.judge` on issue `n`'s report, or None for a maintainer's."""
    issue = store.load_issue(n)
    author = issue.author if issue else None
    if issue is not None and gates.priority_author(author, issue.author_association):
        return None
    on_step("auditing the report")
    blocked = threats.is_blocked_actor(Store().load_threats(), author)
    return intake_audit.judge(spec.title, spec.body, spec.notes, blocked=blocked)


def solve(store: IssueStore, n: int, *, guidance: str | None, trigger: str,
          on_step: Callable[[str], None], notes: str | None = None,
          unattended: bool = False) -> tuple[dict, dict | None]:
    """The cross-tested lane on issue `n`; its record and, for a dispute, the
    drafted question. An `unattended` solve of a report the intake audit reads
    as malicious ends `refused` without running the lane."""
    try:
        base = prove.pinned(Store())
    except prove.NoBase as e:
        raise RequestFailed(str(e)) from e
    spec, report = _spec(store, n, base, guidance, notes)
    workdir = propose.result_dir(n)
    started = storekit.now()
    intake = _intake(store, n, spec, on_step)
    audited = int(intake is not None and not intake.get("blocked"))
    refused = intake_audit.refusal(intake) if unattended else None
    if refused:
        res = fix_lane.LaneResult(ending="refused", fault=False, detail=refused,
                                  result={"intake": intake}, agent_runs=audited)
    else:
        res = cross_lane.run(spec, workdir=workdir, on_step=on_step)
        if intake is not None:
            res.result = {**(res.result or {}), "intake": intake}
            res.agent_runs += audited
    if res.ending == "fixed":
        reason = fix_lane.still_valid_for(n, report)
        if reason:
            res = fix_lane.LaneResult(ending="cancelled", fault=False, detail=reason,
                                      reproduction=res.reproduction, result=res.result,
                                      agent_runs=res.agent_runs)
    record = fix_lane.record_result(
        store, workdir, issue=n, report=report, base=base, action="fix", lane="cross",
        models=list(settings.issue_fix_models()), res=res, started=started,
        finished=storekit.now(), trigger=trigger, extra=_words(guidance, notes))
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
           on_step: Callable[[str], None], trigger: str = "send-back",
           notes: str | None = None) -> dict:
    """One agent revises the last attempt's change under a maintainer's
    `comments` and anyone else's `notes`, proven on the attempt's base or, when
    this machine no longer holds it, the current pin. The revision keeps the
    attempt's `intake`."""
    previous = _last_record(n)
    res_prev = previous.get("result") or {}
    if not res_prev.get("patch"):
        raise RequestFailed("the last attempt holds no change to revise")
    base = _revision_base(previous)
    spec, report = _spec(store, n, base, comments or None, notes)
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
    if res_prev.get("intake") is not None:
        res.result = {**(res.result or {}), "intake": res_prev["intake"]}
    return fix_lane.record_result(
        store, workdir, issue=n, report=report, base=base, action="fix", lane="revise",
        models=[settings.agent_model() or "default"], res=res, started=started,
        finished=storekit.now(), trigger=trigger, extra=_words(comments, notes))


def answer(store: IssueStore, n: int, answer_: dict, *, on_step: Callable[[str], None],
           notes: str | None = None, unattended: bool = False) -> tuple[dict, dict | None]:
    """Resume a dispute on the chosen reading, or re-solve with a written
    answer: a maintainer's in `answer_`, the issue author's in `notes`."""
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
    asked = f"The fix attempt asked: {question.get('question') or '(no question)'}\n"
    return solve(store, n, guidance=asked + f"A maintainer answered: {text}" if text else None,
                 notes=asked + f"The issue's author answered:\n{notes}" if notes else None,
                 trigger="answer", on_step=on_step, unattended=unattended)


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
    issue. Returns the request's ending status (`done`, `failed`, or `cancelled`
    for an attempt someone else's pull request superseded) and the one-line
    outcome. An agent outage propagates, after the request is marked failed."""
    action = req["action"]
    mark = superseded.check(store, n)
    if mark is not None:
        outcome = f"Stepped aside: {fix_review.rival_open(mark)}"
        _finish(store, n, req, "cancelled", outcome)
        _note(store, n, outcome)
        return "cancelled", outcome
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
    mark = superseded.check(store, n)
    if mark is not None:
        _note(store, n, f"Stepped aside: {fix_review.rival_open(mark)}")
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
    notes = req.get("notes")
    trigger = "hunter" if req.get("source") == "hunter" else "operator"
    unattended = req.get("source") in ("hunter", "public")
    if action == "solve":
        record, question = solve(store, n, guidance=guidance, notes=notes, trigger=trigger,
                                 on_step=on_step, unattended=unattended)
        _write_run(store, n, record, question)
        return f"Solved: {record['ending']} — {record['detail']}"
    if action == "send-back" and req.get("source") == "followup":
        return _revise_proposal(store, n, guidance or "", notes=notes, trigger="followup",
                                live=settings.issue_fix_followup() == "live", on_step=on_step)
    if action == "send-back":
        issue = store.load_issue(n)
        pr = fix_review.open_pr(issue) if issue else None
        mode = route(guidance or "")
        on_step(f"the comments ask to {mode}")
        if pr and mode == "restart":
            raise RequestFailed(f"the comments ask to start over, but #{pr} is open with this "
                                f"fix; a send-back revises the change on #{pr}, so say what "
                                "to change in it")
        if pr:
            return _revise_proposal(store, n, guidance or "", notes=notes, trigger="send-back",
                                    live=not req.get("dry_run"), on_step=on_step)
        if mode == "restart":
            record, question = solve(store, n, guidance=guidance, notes=notes,
                                     trigger="send-back", on_step=on_step)
            _write_run(store, n, record, question)
            return f"Started over: {record['ending']} — {record['detail']}"
        record = revise(store, n, comments=guidance or "", notes=notes, on_step=on_step)
        _write_run(store, n, record, None)
        return f"Revised: {record['ending']} — {record['detail']}"
    if action == "answer":
        record, question = answer(store, n, req.get("answer") or {}, notes=notes,
                                  on_step=on_step, unattended=unattended)
        _write_run(store, n, record, question)
        return f"Resumed on the answer: {record['ending']} — {record['detail']}"
    if action in ("ask-reporter", "propose"):
        return _bot_action(store, n, action, dry_run=bool(req.get("dry_run")))
    raise RequestFailed(f"unknown action {action!r}")


def _revise_proposal(store: IssueStore, n: int, guidance: str, *, trigger: str, live: bool,
                     on_step: Callable[[str], None], notes: str | None = None) -> str:
    """Revise the fix behind issue `n`'s open pull request under `guidance`
    and `notes` and, when the revision ends `fixed`, push it onto the same branch
    (`executor.update_issue_fix_proposal`) and set the follow-up watching the
    new head. A revision that ends any other way, or does not reach the pull
    request, leaves the pull request, the run it shows, and the result on disk
    as they were. Everything but the writes when not `live`."""
    from prospector_app.backend import executor

    issue = store.load_issue(n)
    pr = fix_review.open_pr(issue) if issue else None
    if issue is None or pr is None:
        raise RequestFailed("the attempt has no open pull request to revise")
    proposal = (issue.fix_run or {})["proposal"]
    result_file = propose.result_dir(n) / "result.json"
    kept = result_file.read_text()
    try:
        record = revise(store, n, comments=guidance, notes=notes, on_step=on_step,
                        trigger=trigger)
    except BaseException:
        result_file.write_text(kept)
        raise
    if record.get("ending") != "fixed":
        result_file.write_text(kept)
        return (f"The revision ended {record.get('ending')}: {record.get('detail')}; "
                f"#{pr} is unchanged")
    on_step("pushing the revision")
    token = executor.mint_bot_token() if live else None
    res = executor.update_issue_fix_proposal(n, pr, push=True, token=token, dry_run=not live)
    if res.get("status") in ("blocked", "error"):
        result_file.write_text(kept)
        raise RequestFailed(f"the revision could not go onto #{pr}: {res.get('detail')}")
    if res.get("status") == "dry-run":
        result_file.write_text(kept)
        return f"Dry run: {res.get('detail')}"
    distilled = fix_review.distill(record)
    distilled["proposal"] = proposal
    edit = store.edit_issue(n)
    edit.record_fix_run(distilled)
    fu = fix_review.followup_for(edit.fix_followup, pr)
    if fu:
        edit.record_fix_followup({**fu, "state": "watching",
                                  "reason": "revision pushed; following up the new head"})
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
