"""Carry out one issue-fix request (`fix_review.queue`) on the machine that holds
the sandbox, and write what happened back to the issue.

- `solve` runs the cross-tested lane with the request's guidance and notes
  (a maintainer's instruction, and anyone else's words as data), first having
  `intake_audit` judge an outsider's report: an unattended solve of a report it
  reads as malicious ends `refused` with no lane run. A fixed run then faces the
  tests of other authors' pull requests on the issue (`second_opinion`) and is
  revised once when any fails with it; a dispute
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
  whose fixed run faces the same `second_opinion`, or re-solves with the question and a written answer: a maintainer's as
  guidance, the issue author's as notes.
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
    followup,
    intake_audit,
    issue_display,
    issue_gates,
    propose,
    related_prs,
    second_opinion,
    solo_lane,
)
from issue_triage.issue_store import IssueStore
from pipeline import gates, headless_agent, prove, settings, storekit, threats, verdict_log
from pipeline.store import Store

# A follow-up revision answers the pull request's reviewers; the design lens's
# objections to it go to the next round instead of stopping the push.
FOLLOWUP_ADVISORY = frozenset({"design"})

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
                              notes=notes, verdict_subject=verdict_log.Subject("issue", n)),
            fix_lane.report_sha(title, body))


def _words(guidance: str | None, notes: str | None) -> dict[str, str] | None:
    """What a run record keeps of the words the request carried."""
    return {k: v for k, v in (("guidance", guidance), ("notes", notes)) if v} or None


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
    return intake_audit.judge(spec.title, spec.body, spec.notes, blocked=blocked,
                              subject=verdict_log.Subject("issue", n))


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
    if record["ending"] == "fixed":
        record = _second_opinion(store, n, base, record, trigger=trigger, on_step=on_step)
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


def _second_opinion(store: IssueStore, n: int, base: prove.PinnedBase, record: dict, *,
                    trigger: str, on_step: Callable[[str], None]) -> dict:
    """`record`, a fixed run, judged by the tests of other authors' pull
    requests on issue `n` (`second_opinion`), and revised once when any fails
    with it. The revision replaces the fix only when it ends `fixed`. The run
    written carries every rival's entry."""
    rivals, entries = second_opinion.rivals(n, exclude=set(), registry=Store().load_threats())
    if not rivals and not entries:
        return record
    title, body = fix_lane.reported_text(store, n) or ("", "")
    label = f"second-{n}"
    gaps: list[tuple[second_opinion.Rival, dict]] = []
    for rival in rivals:
        on_step(f"second opinion: #{rival.pr}'s tests")
        entry = second_opinion.judge(base, rival, record["result"]["patch"], label=label,
                                     title=title, body=body)
        entries.append(entry)
        if entry["verdict"] == "gap":
            gaps.append((rival, entry))
    result_file = propose.result_dir(n) / "result.json"
    if gaps:
        kept = result_file.read_text()
        on_step("revising against the other pull requests' tests")
        revised = revise(store, n, comments="", notes=second_opinion.notes(gaps),
                         on_step=on_step, trigger=trigger)
        if revised["ending"] == "fixed":
            record = revised
        else:
            result_file.write_text(kept)
        for rival, entry in gaps:
            entries[entries.index(entry)] = second_opinion.recheck(
                base, rival, record["result"]["patch"], entry, label=label, title=title,
                body=body)
    record["result"]["second_opinion"] = entries
    result_file.write_text(json.dumps(record, indent=2) + "\n")
    return record


def revise(store: IssueStore, n: int, *, comments: str,
           on_step: Callable[[str], None], trigger: str = "send-back",
           notes: str | None = None) -> dict:
    """One agent revises the last attempt's change under a maintainer's
    `comments` and anyone else's `notes`, proven on the attempt's base or, when
    this machine no longer holds it, the current pin. The revision keeps the
    attempt's holds (`trust_boundary.held`): its `intake`, its lone
    reproduction, and its `second_opinion`, which a caller that rechecks the
    rivals replaces."""
    previous = _last_record(n)
    res_prev = previous.get("result") or {}
    if not res_prev.get("patch"):
        raise RequestFailed("the last attempt holds no change to revise")
    base = _revision_base(previous)
    spec, report = _spec(store, n, base, comments or None, notes)
    review = "\n".join(f"- {c}" for c in
                       issue_gates.review_comments(res_prev.get("reviews"),
                                                   res_prev.get("claims"))[1])
    workdir = propose.result_dir(n)
    started = storekit.now()
    res = solo_lane.run(spec, workdir=workdir, on_step=on_step, start_patch=res_prev["patch"],
                        attempt={"summary": res_prev.get("summary"),
                                 "root_cause": res_prev.get("root_cause"),
                                 "review": review or None},
                        review=True,
                        advisory=FOLLOWUP_ADVISORY if trigger == "followup" else frozenset())
    kept = {k: res_prev[k] for k in ("intake", "second_opinion") if res_prev.get(k) is not None}
    if (res_prev.get("agreement") or {}).get("single"):
        kept["agreement"] = {"single": True}
    if kept:
        res.result = {**(res.result or {}), **kept}
    return fix_lane.record_result(
        store, workdir, issue=n, report=report, base=base, action="fix", lane="revise",
        models=[settings.agent_model() or "default"], res=res, started=started,
        finished=storekit.now(), trigger=trigger, extra=_words(comments, notes))


def answer(store: IssueStore, n: int, answer_: dict, *, on_step: Callable[[str], None],
           notes: str | None = None, unattended: bool = False) -> tuple[dict, dict | None]:
    """Resume a dispute on the chosen reading, or re-solve with a written
    answer: a maintainer's in `answer_`, the issue author's in `notes`. A
    chosen reading whose base this machine no longer holds is re-solved with
    that reading as the maintainer's answer."""
    previous = _last_record(n)
    qpath = propose.result_dir(n) / "question.json"
    saved = json.loads(qpath.read_text()) if qpath.exists() else {}
    question = saved.get("question") or {}
    options = [o["label"] for o in question.get("options") or []]
    label = answer_.get("label")
    text = str(answer_.get("text") or "").strip()
    base: prove.PinnedBase | None = None
    if label and label in options:
        try:
            base = prove.held(previous["base_sha"], int(previous.get("base_tier") or 0))
        except prove.NoBase:
            behavior = question["options"][options.index(label)].get("behavior")
            text = "\n".join(w for w in (f"{label}: {behavior}" if behavior else label, text) if w)
    if base is not None:
        readings = (previous.get("result") or {}).get("readings") or []
        reading = readings[options.index(label)]
        spec, report = _spec(store, n, base, None)
        workdir = propose.result_dir(n)
        started = storekit.now()
        res = cross_lane.judge_reading(spec, workdir=workdir, result=previous["result"],
                                       reading=reading, on_step=on_step)
        record = fix_lane.record_result(
            store, workdir, issue=n, report=report, base=base, action="fix", lane="cross",
            models=previous.get("models") or [], res=res, started=started,
            finished=storekit.now(), trigger="answer", extra={"answer": answer_})
        if record["ending"] == "fixed":
            record = _second_opinion(store, n, base, record, trigger="answer", on_step=on_step)
        return record, {**question, "answered": answer_}
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
    issue. Returns the request's ending status (`done` or `failed`) and the
    one-line outcome. An agent outage propagates, after the request is marked
    failed."""
    action = req["action"]
    try:
        outcome = _carry_out(store, n, action, req, on_step=on_step)
        status = "done"
    except headless_agent.AgentUnavailable as e:
        _finish(store, n, req, "failed", f"the agent is unavailable: {e}")
        raise
    except (RequestFailed, prove.NoBase, prove.SandboxFault, RuntimeError, ValueError) as e:
        outcome, status = f"{action} failed: {e}", "failed"
    _finish(store, n, req, status, outcome)
    _note(store, n, outcome)
    issue_display.write_story(store, n, trigger=action)
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
        return f"Solved: {fix_review.outcome(record)}"
    if action == "send-back" and req.get("rivals"):
        return _late_rivals(store, n, [int(p) for p in req["rivals"]],
                            live=settings.issue_fix_followup() == "live", on_step=on_step)
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
            return f"Started over: {fix_review.outcome(record)}"
        record = revise(store, n, comments=guidance or "", notes=notes, on_step=on_step)
        _write_run(store, n, record, None)
        return f"Revised: {fix_review.outcome(record)}"
    if action == "answer":
        record, question = answer(store, n, req.get("answer") or {}, notes=notes,
                                  on_step=on_step, unattended=unattended)
        _write_run(store, n, record, question)
        return f"Resumed on the answer: {fix_review.outcome(record)}"
    if action in ("ask-reporter", "propose"):
        return _bot_action(store, n, action, dry_run=bool(req.get("dry_run")))
    raise RequestFailed(f"unknown action {action!r}")


def _revise_proposal(store: IssueStore, n: int, guidance: str, *, trigger: str, live: bool,
                     on_step: Callable[[str], None], notes: str | None = None,
                     amend: Callable[[dict], bool] | None = None) -> str:
    """Revise the fix behind issue `n`'s open pull request under `guidance`
    and `notes` and, when the revision ends `fixed` and `amend` (given the
    revised record to complete) passes it, push it onto the same branch
    (`executor.update_issue_fix_proposal`) and set the follow-up watching the
    new head. A revision that ends any other way, or does not reach the pull
    request, leaves the pull request, the run it shows, and the result on disk
    as they were; one that leaves the code as it was hands the pull request back
    with the fixer's answers. The follow-up's own revision (`trigger`
    "followup") is counted when it finishes (`followup.count_revision`), and the
    design lens only comments on it (FOLLOWUP_ADVISORY). An operator's send-back (`trigger` "send-back") that ends
    without a fix, or pushes one that still leaves part of the report
    (`fix_review.partial_note`), hands the pull request back to them; that hand-back
    is recorded in the store in a dry run too, like the follow-up's own. Everything
    but the upstream writes when not `live`."""
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
        outcome = (f"The revision ended {record.get('ending')}: {record.get('detail')}; "
                   f"#{pr} is unchanged")
        if trigger == "send-back":
            _hand_back(store, n, pr, outcome)
        elif trigger == "followup":
            _count_revision(store, n, pr, record, pushed=False)
        return outcome
    if amend is not None and not amend(record):
        result_file.write_text(kept)
        return f"The revision does not answer what it was for; #{pr} is unchanged"
    if (record.get("result") or {}).get("patch") == (json.loads(kept).get("result") or {}).get("patch"):
        result_file.write_text(kept)
        answers = "; ".join(f'{r.get("answer")} "{r.get("comment")}": {r.get("why")}'
                            for r in (record.get("result") or {}).get("responses") or [])
        outcome = (f"The revision left the code as it was; #{pr} is unchanged. The fixer's "
                   f"answer: {answers or 'none given'}")
        _hand_back(store, n, pr, outcome)
        return outcome
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
        if trigger == "followup":
            fu = followup.count_revision(fu, record, pushed=True)
        edit.record_fix_followup({**fu, "state": "watching",
                                  "reason": "revision pushed; following up the new head"})
    left = fix_review.partial_note(distilled) if trigger == "send-back" else None
    if left:
        _hand_back(store, n, pr, f"the revision still leaves part of the report: {left}",
                   head_sha=res.get("head_sha"))
    return f"Revised #{pr}: {res.get('detail')}"


def _count_revision(store: IssueStore, n: int, pr: int, record: dict, *,
                    pushed: bool) -> None:
    edit = store.edit_issue(n)
    fu = fix_review.followup_for(edit.fix_followup, pr)
    if fu:
        edit.record_fix_followup(followup.count_revision(fu, record, pushed=pushed))


def _hand_back(store: IssueStore, n: int, pr: int, reason: str, *,
               head_sha: object = None) -> None:
    """Hand issue `n`'s pull request `pr` back to the operator for `reason`, a
    failing the fixer owns."""
    edit = store.edit_issue(n)
    fu = {"pr": pr, **fix_review.followup_for(edit.fix_followup, pr)}
    head = head_sha or fu.get("head_sha")
    fu.pop("approved", None)
    edit.record_fix_followup({**fu, "state": "handed-back", "reason": reason[:400],
                              **({"head_sha": head} if head else {}),
                              "judged": {"head_sha": head, "reason": reason[:400],
                                         "outside": False}})


def _late_rivals(store: IssueStore, n: int, prs: list[int], *, live: bool,
                 on_step: Callable[[str], None]) -> str:
    """Judge the fix behind issue `n`'s open pull request by the tests of the
    other authors' pull requests `prs` (`second_opinion`), and revise it onto
    the pull request when that closes a gap they find. The entries join the
    run's `second_opinion`; a gap left open hands the pull request back."""
    record = _last_record(n)
    base = _revision_base(record)
    registry = Store().load_threats()
    related = {r["number"]: r for r in related_prs.search(n) or []}
    title, body = fix_lane.reported_text(store, n) or ("", "")
    label = f"second-{n}"
    entries: list[dict] = []
    gaps: list[tuple[second_opinion.Rival, dict]] = []
    for pr in prs:
        if pr not in related:
            entries.append({"pr": pr, "author": None, "source": "pr", "verdict": "skipped",
                            "why": "it no longer names the issue"})
            continue
        loaded = second_opinion.load(related[pr], registry)
        if isinstance(loaded, second_opinion.Rival):
            on_step(f"second opinion: #{pr}'s tests")
            loaded_entry = second_opinion.judge(base, loaded, record["result"]["patch"],
                                                label=label, title=title, body=body)
            if loaded_entry["verdict"] == "gap":
                gaps.append((loaded, loaded_entry))
            entries.append(loaded_entry)
        else:
            entries.append(loaded)
    prior = list(record["result"].get("second_opinion") or [])
    result_file = propose.result_dir(n) / "result.json"

    def amend(revised: dict) -> bool:
        for rival, entry in gaps:
            entries[entries.index(entry)] = second_opinion.recheck(
                base, rival, revised["result"]["patch"], entry, label=label, title=title,
                body=body)
        revised["result"]["second_opinion"] = prior + entries
        result_file.write_text(json.dumps(revised, indent=2) + "\n")
        return any(e["verdict"] == "gap-closed" for e in entries)

    outcome = "; ".join(f"#{e['pr']} {e['verdict']}" for e in entries)
    if gaps:
        outcome = _revise_proposal(store, n, "", notes=second_opinion.notes(gaps),
                                   trigger="followup", live=live, on_step=on_step, amend=amend)
    entries[:] = [{**e, "verdict": "gap-open", "why": f"{e['why']}; no revision passed them"}
                  if e["verdict"] == "gap" else e for e in entries]
    current = _last_record(n)
    if current["result"].get("second_opinion") != prior + entries:
        current["result"]["second_opinion"] = prior + entries
        result_file.write_text(json.dumps(current, indent=2) + "\n")
    edit = store.edit_issue(n)
    edit.record_fix_run({**(edit.fix_run or {}), "second_opinion": prior + entries})
    held = second_opinion.flag(entries)
    pr = fix_review.open_pr(edit)
    fu = fix_review.followup_for(edit.fix_followup, pr)
    if held and fu:
        kept = {k: v for k, v in fu.items() if k != "approved"}
        edit.record_fix_followup({**kept, "state": "handed-back", "reason": held[:400],
                                  "judged": {"head_sha": fu.get("head_sha"),
                                             "reason": held[:400], "outside": False}})
    return f"Second opinion: {outcome}"


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
