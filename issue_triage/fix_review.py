"""The ONE policy for reviewing issue fixes in the app: what an operator may ask
of an issue's fix attempt, the status the attempt is in, and the attempt as the
app shows it.

An issue holds at most one pending request (`fix_request`, actions
`issue_store.ISSUE_FIX_ACTIONS`), which `queue` admits only when it fits the
latest attempt (`fix_run`) and no other request is in flight; the words an
operator gives with it land in the thread (`fix_thread`). `fix_status` derives,
on read, whose move the attempt waits on; a proposal's follow-up record
(`fix_followup`) counts only while it follows the run's own pull request.
A pull request the follow-up finds green is left to the maintainers unless it
is a partial fix, which waits for an operator. An operator's approval
(`approve`) leaves an open pull request to the maintainers: it rides on the follow-up record and holds (`approval`) through
later heads and revisions, until the follow-up hands the pull request back for
something new the fixer failed at — not for CI that fails outside the change.
`distill` turns a finished run's `result.json` record into the `fix_run` the app
renders: each candidate's own account, the agreement, the picked change, the
checks, the reviewers, the rounds of code review it was revised in and the
tests that put their claims, and the question — never an agent's transcript. A fixed
run that covers only part of its report (`issue_gates.fix_coverage`) reads as
a partial fix wherever the attempt is named (`outcome`, `fix_status`).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from issue_triage import (
    dispute_question,
    fix_choice,
    fix_pr_body,
    followup,
    issue_gates,
    second_opinion,
    trust_boundary,
)
from pipeline import diffpaths, gates, reviewers, settings, storekit

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from issue_triage.issue_store import IssueStore

IN_FLIGHT = ("queued", "running")
FAULT_ENDINGS = ("agent-unavailable", "run-failed", "sandbox", "base-compile")
# The largest picked change `fix_run` carries; a longer one is cut and flagged.
PATCH_MAX = 60_000
# The status of an issue's fix attempt, in the order the explorer groups them.
STATUSES = ("review", "question", "running", "reporter", "pr-open", "chosen", "pr-closed",
            "failed", "declined", "pr-merged")


def followup_for(fu: dict | None, pr: object) -> dict:
    """A copy of the follow-up record `fu` when it follows pull request `pr`,
    else an empty one."""
    if not fu or pr is None or str(fu.get("pr")) != str(pr):
        return {}
    return dict(fu)


def _ended(proposal: dict, fu: dict) -> tuple[str, str, str] | None:
    """(status, reason, line) for a proposal whose follow-up saw it end, or
    None while it is open. Only `closed_as: merged` reads as merged; a record
    without `closed_as` reads as closed, in the follow-up's own words."""
    if fu.get("state") != "done":
        return None
    pr = proposal["pr"]
    if fu.get("closed_as") == "merged":
        return "pr-merged", f"#{pr} merged", f"Prospector's fix #{pr} was merged."
    if fu.get("closed_as") == "closed":
        return ("pr-closed", f"#{pr} was closed without merging",
                f"Prospector's fix #{pr} was closed without merging.")
    return ("pr-closed", str(fu.get("reason") or f"#{pr} is no longer open"),
            f"Prospector's fix #{pr} is no longer open.")


def partial_note(run: dict) -> str | None:
    """What a fixed run whose coverage is partial leaves of its report, or None
    for any other run. Defects the base already handles count as handled; those
    no claim test showed remain are named apart."""
    if run.get("ending") != "fixed":
        return None
    coverage = issue_gates.fix_coverage(run.get("reviews"), run.get("claims"))
    if coverage.state != "partial":
        return None
    left = coverage.having("not-addressed")
    already = coverage.having("already-fixed")
    return (f"partial fix: {len(coverage.defects) - len(left)} of {len(coverage.defects)} "
            "reported defects handled"
            + (f", {len(already)} of them already on the base" if already else "")
            + "; not addressed: "
            + fix_pr_body.inert("; ".join(d["defect"] for d in left))
            + ("; not shown to remain: " + fix_pr_body.inert("; ".join(
                d["defect"] for d in coverage.unshown)) if coverage.unshown else ""))


def outcome(record: dict) -> str:
    """A finished run's ending in the words the thread uses: a partial fix
    names itself."""
    result = record.get("result") or {}
    note = partial_note({**record, "reviews": result.get("reviews"),
                         "claims": result.get("claims")})
    return f"{note} — {record['detail']}" if note else f"{record['ending']} — {record['detail']}"


def partial(issue: Issue) -> str | None:
    """`partial_note` while the attempt waits on a review or an open pull
    request, the statuses that present it as a fix, or while a request runs
    against it."""
    status = _fix_status(issue)
    return partial_note(issue.fix_run or {}) if status and status[0] in ("review", "pr-open",
                                                                          "running") else None


def fix_status(issue: Issue) -> tuple[str, str] | None:
    """(status, reason) for issue's fix attempt, or None when it has none. The
    reason of a partial fix leads with what it leaves (`partial`)."""
    status = _fix_status(issue)
    if status is None:
        return None
    note = partial(issue)
    return status[0], f"{note} — {status[1]}" if note else status[1]


class StatusPointer(TypedDict):
    section: str  # the issue page's section: "rivals"
    phrase: str   # the words in the sentence that link to it


class StatusLine(TypedDict):
    text: str
    see: StatusPointer | None


def status_line(issue: Issue) -> StatusLine:
    """Where issue stands, in the one plain sentence its page leads with, and
    the phrase in it that points at a section of the page, if any."""
    status = _fix_status(issue)
    if status is None:
        return {"text": "This issue is closed." if issue.state != "open"
                else "Prospector hasn't tried to fix this yet.", "see": None}
    line = status[2] if isinstance(status[2], dict) else StatusLine(text=status[2], see=None)
    return {**line, "text": line["text"] + (" It fixes only part of the report." if partial(issue) else "")}


def _blocked_by(judged: dict) -> str | None:
    """Whose bar a hand-back's change could not clear (`followup.Step.by`). A
    judgment without `by` names its blocker in its reason."""
    if judged.get("by"):
        return str(judged["by"])
    reason = str(judged.get("reason") or "")
    if reason.endswith("CI still fails"):
        return followup.CI
    named = [r.label for r in reviewers.REVIEWERS.values() if r.id in reason.lower()]
    return " and ".join(named) or None


def _handed_back_line(pr: object, fu: dict) -> str:
    judged = fu.get("judged") or {}
    why = judged.get("reason") or fu.get("reason") or ""
    by = _blocked_by(judged)
    revisions = int(fu.get("revisions") or 0)
    tries = (f", and {revisions} revision{'s' if revisions != 1 else ''} didn't fix it"
             if revisions else "")
    if by == followup.CI:
        return (f"Prospector's fix #{pr} fails CI for a reason outside its change." if judged.get("outside")
                else f"Prospector's fix #{pr} fails CI{tries}.")
    if by == followup.MAINTAINER:
        return f"A maintainer asked for a change to Prospector's fix #{pr} that it couldn't make{tries}."
    if by:
        return f"Prospector's fix #{pr} failed the {by} review{tries}."
    return f"Prospector's fix #{pr} came back to you: {why}."


def _held_line(issue: Issue) -> str | StatusLine:
    held = trust_boundary.held(issue)
    rivals = (issue.fix_run or {}).get("second_opinion")
    if held is None:
        return "Prospector has a working fix that is about to open as a PR."
    if held == second_opinion.flag(rivals):
        text, phrase = second_opinion.held_line(rivals)
        return {"text": text, "see": {"section": "rivals", "phrase": phrase}}
    return f"Prospector has a working fix but is holding it: {held}."


def _fix_status(issue: Issue) -> tuple[str, str, str | StatusLine] | None:
    """(status, reason, line): the status, its terse reason, and the plain
    sentence `status_line` gives."""
    pr = fix_choice.chosen(issue)
    if pr is not None:
        return ("chosen", f"going with #{pr}",
                f"You chose #{pr} as the fix, so Prospector stopped its own.")
    req, run = issue.fix_request, issue.fix_run
    if req and req.get("status") in IN_FLIGHT:
        working: str | StatusLine = "Prospector is working on a fix right now."
        if trust_boundary.held(issue):
            line = _held_line(issue)
            working = (StatusLine(text=f"{line['text']} {working}", see=line["see"])
                       if isinstance(line, dict) else f"{line} {working}")
        return "running", f"{req.get('action')} {req.get('status')}", working
    broke = "Prospector's last fix attempt broke down before it finished."
    if req and req.get("status") == "failed":
        return "failed", str(req.get("reason") or f"{req.get('action')} failed"), broke
    if not run:
        return None
    proposal = run.get("proposal") or {}
    if proposal.get("pr"):
        fu = followup_for(issue.fix_followup, proposal["pr"])
        ended = _ended(proposal, fu)
        if ended:
            return ended
        pr = proposal["pr"]
        why = (fu.get("judged") or {}).get("reason") or fu.get("reason") or ""
        held = approval(issue)
        if held:
            return ("pr-open", _approved_reason(pr, held, fu),
                    f"You approved Prospector's fix #{pr}; it's with the maintainers."
                    + (f" It came back once: {why}." if fu.get("state") == "handed-back" else ""))
        if fu.get("state") == "ready":
            if partial_note(run) is None:
                return ("pr-open", f"#{pr} is green; with the maintainers",
                        f"Prospector's fix #{pr} passes its checks; it's with the maintainers.")
            return ("review", f"#{pr} is green — ready for your review",
                    f"Prospector's fix #{pr} passes its checks and waits for your review.")
        if fu.get("state") == "handed-back":
            return "review", f"#{pr} handed back: {why}", _handed_back_line(pr, fu)
        if fu.get("state") == "watching" and fu.get("reason"):
            return "pr-open", f"#{pr}: {fu['reason']}", f"Prospector's fix is open as #{pr}: {fu['reason']}."
        return "pr-open", f"#{pr}", f"Prospector's fix is open as #{pr}."
    ending, detail = run.get("ending"), str(run.get("detail") or "")
    if ending == "fixed":
        return "review", detail, _held_line(issue)
    if ending == "fix-disputed":
        question = run.get("question") or {}
        if question.get("asked"):
            return ("reporter", "a question is waiting on the reporter",
                    "Prospector asked the reporter what the report wants and is waiting for an answer.")
        if question.get("question"):
            return ("question", str(question["question"]),
                    "Prospector's fix attempts disagree on what the report wants; answer their question to go on.")
        return ("declined", "the agents disagreed and no question could be drafted",
                "Prospector's fix attempts disagree on what the report wants and couldn't say why.")
    if ending in FAULT_ENDINGS or run.get("fault"):
        return "failed", f"{ending}: {detail}", broke
    return "declined", f"{ending}: {detail}", "Prospector tried to fix this and couldn't."


def machine_fault(issue: Issue) -> str | None:
    """What in Prospector itself broke on issue's latest fix request or run —
    a request that failed, or a run that ended on the machine's condition
    (FAULT_ENDINGS) — or None."""
    req, run = issue.fix_request or {}, issue.fix_run or {}
    if req.get("status") == "failed":
        return str(req.get("reason") or f"{req.get('action')} failed")
    if run.get("fault") or run.get("ending") in FAULT_ENDINGS:
        return f"{run.get('ending')}: {run.get('detail') or ''}".strip()
    return None


def approval(issue: Issue) -> dict | None:
    """The operator's approval of the open pull request while it holds: through
    watching, a green head, and a hand-back the operator approved over or one
    for CI that fails outside the change (`judged.outside`). A hand-back for
    anything else after the approval ends it."""
    pr = open_pr(issue)
    fu = followup_for(issue.fix_followup, pr)
    held = fu.get("approved")
    if not held:
        return None
    judged = fu.get("judged")
    if (fu.get("state") == "handed-back" and judged != held.get("judged")
            and not (judged or {}).get("outside")):
        return None
    return held


def _approved_reason(pr: object, held: dict, fu: dict) -> str:
    reason = f"#{pr} approved by {held.get('by')}" + (" as a partial fix" if held.get("partial")
                                                       else "") + "; with the maintainers"
    if fu.get("state") == "handed-back":
        reason += f" — {(fu.get('judged') or {}).get('reason') or fu.get('reason') or ''}"
    return reason


def approve(store: IssueStore, n: int, *, by: str, partial: bool) -> tuple[bool, str]:
    """Approve issue `n`'s open pull request as it stands and leave it to the
    maintainers (`approval`). A `partial` approval asks the follow-up to
    re-render the description, which then names the issue without a closing
    keyword. (ok, reason)."""
    issue = store.load_issue(n)
    if issue is None:
        return False, f"issue #{n} is not in the store"
    req = issue.fix_request
    if req and req.get("status") in IN_FLIGHT:
        return False, f"a {req.get('action')} request is already {req.get('status')}"
    pr = open_pr(issue)
    if pr is None:
        return False, "the attempt has no open pull request to approve"
    fu = {"pr": pr, "state": "watching", **followup_for(issue.fix_followup, pr)}
    fu["approved"] = {"by": by, "at": storekit.now(), "judged": fu.get("judged"),
                      "partial": bool(partial)}
    if partial:
        fu["described_head"] = ""
    issue.record_fix_followup(fu)
    issue.append_fix_thread({"at": fu["approved"]["at"], "by": by, "kind": "comment",
                             "action": "approve",
                             "text": f"Approved #{pr}" + (" as a partial fix" if partial else "")
                                     + "; left for the maintainers."})
    return True, f"#{pr} approved"


def open_pr(issue: Issue) -> int | None:
    """The pull request issue's fix attempt is proposed as, until its follow-up
    sees it merged or closed. A send-back revises the change on it, and the
    attempt cannot be replaced while it is open."""
    pr = ((issue.fix_run or {}).get("proposal") or {}).get("pr")
    if not pr or followup_for(issue.fix_followup, pr).get("state") == "done":
        return None
    return int(pr)


def _fits(action: str, run: dict | None, *, guidance: str | None, answer: dict | None,
          pr: int | None, followup: dict | None = None, notes: str | None = None,
          rivals: list[int] | None = None) -> str | None:
    """Why `action` does not fit the latest attempt `run`, open as pull request
    `pr` and followed up in `followup`, or None when it does."""
    if action == "solve":
        return f"#{pr} is open with this attempt's fix; send it back to change it" if pr else None
    if not run:
        return "there is no fix attempt to act on"
    ending = run.get("ending")
    question = run.get("question") or {}
    if action == "send-back":
        if not run.get("patch"):
            return "the attempt holds no change to send back"
        if not (guidance or "").strip() and not (notes or "").strip() and not rivals:
            return "sending a fix back needs your comments"
        return None
    if action == "answer":
        if ending != "fix-disputed" or not question.get("options"):
            return "the attempt asks no question"
        labels = [o["label"] for o in question["options"]]
        if ((answer or {}).get("label") in labels or str((answer or {}).get("text") or "").strip()
                or (notes or "").strip()):
            return None
        return f"an answer names one of {labels} or says what should happen"
    if action == "ask-reporter":
        if ending != "fix-disputed" or not question.get("question"):
            return "the attempt asks no question"
        if question.get("asked"):
            return "the reporter was already asked"
        return None
    if action == "propose":
        if ending != "fixed":
            return f"the attempt ended {ending!r}, not 'fixed'"
        proposal = run.get("proposal") or {}
        if proposal.get("pr"):
            ended = _ended(proposal, followup_for(followup, proposal["pr"]))
            if ended:
                return (f"this change already merged as #{proposal['pr']}"
                        if ended[0] == "pr-merged" else f"{ended[1]}; try again for a new change")
            return "a pull request is already open"
        return None
    return f"unknown action {action!r}"


def queue(store: IssueStore, n: int, action: str, *, by: str, source: str = "operator",
          guidance: str | None = None, answer: dict | None = None,
          notes: str | None = None, rivals: list[int] | None = None,
          dry_run: bool = False, feature: bool = False) -> tuple[bool, str]:
    """Queue `action` on issue `n`'s fix attempt for the worker, recording the
    words it carries in the thread. `guidance` is an operator's or maintainer's
    instruction; `notes` are words from anyone else — the issue's author, the
    code reviewers, CI — which the fix agent weighs as data. `rivals` names other
    authors' pull requests whose tests a `send-back` judges the fix by. A request that
    retries a failed one of the same action carries its attempt count on. The
    request, follow-up edit and thread entry land in one compare-and-swap, so a
    request that lands first is never overwritten. `feature` marks a request a
    maintainer chose to implement as a feature. (ok, reason)."""
    got = store.stamped_issue(n)
    if got is None:
        return False, f"issue #{n} is not in the store"
    issue, stamp = got
    if (issue.raw.get("meta") or {}).get("state") != "open":
        return False, f"issue #{n} is closed"
    chosen = fix_choice.chosen(issue)
    if chosen is not None:
        return False, f"an operator chose #{chosen} as the fix"
    req = issue.fix_request
    if req and req.get("status") in IN_FLIGHT:
        return False, f"a {req.get('action')} request is already {req.get('status')}"
    why = _fits(action, issue.fix_run, guidance=guidance, answer=answer, pr=open_pr(issue),
                followup=issue.fix_followup, notes=notes, rivals=rivals)
    if why:
        return False, why
    retry = bool(req and req.get("status") == "failed" and req.get("action") == action)
    section: dict = {"action": action, "status": "queued", "source": source,
                     "requested_by": by, "queued_at": storekit.now(), "dry_run": dry_run,
                     "attempts": int((req or {}).get("attempts") or 1) + 1 if retry else 1}
    if guidance and guidance.strip():
        section["guidance"] = guidance.strip()
    if answer:
        section["answer"] = answer
    if notes and notes.strip():
        section["notes"] = notes.strip()
    if rivals:
        section["rivals"] = list(rivals)
    if feature:
        section["feature"] = True
    issue.stage_fix_request(section)
    fu = issue.fix_followup
    if action == "send-back" and source == "operator" and fu and "approved" in fu:
        issue.stage_fix_followup({k: v for k, v in fu.items() if k != "approved"})
    given = answer or {}
    words = "\n\n".join(w for w in ((guidance or "").strip(), str(given.get("text") or "").strip(),
                                    (notes or "").strip()) if w)
    if given.get("label"):
        words = f"Answered {given['label']}" + (f": {words}" if words else "")
    if words:
        issue.stage_fix_thread({"at": section["queued_at"], "by": by, "source": source,
                                "kind": "answer" if action == "answer" else "comment",
                                "action": action, "text": words})
    if not store.save_issue_if(issue, stamp):
        return False, "another request landed first"
    return True, f"{action} queued"


def cancel(store: IssueStore, n: int, *, by: str) -> tuple[bool, str]:
    """Cancel issue `n`'s queued request; a running one finishes."""
    issue = store.load_issue(n)
    req = issue.fix_request if issue else None
    if not req or req.get("status") != "queued":
        return False, "no queued request to cancel"
    assert issue is not None
    issue.record_fix_request({**req, "status": "cancelled", "finished_at": storekit.now(),
                              "reason": f"cancelled by {by}"})
    return True, "cancelled"


def queued_issues(issues: dict[int, Issue]) -> list[int]:
    """Issues with a queued request, an operator's priority first (higher
    first), then a maintainer's (gates.priority_author), then oldest first."""
    rows = [(-float((i.fix_request or {}).get("priority") or 0),
             not gates.priority_author(i.author, i.author_association),
             (i.fix_request or {}).get("queued_at") or "", n) for n, i in issues.items()
            if (i.fix_request or {}).get("status") == "queued"]
    return [n for *_, n in sorted(rows)]


def _proof(proof: dict) -> dict:
    out: dict = {}
    compiled = proof.get("compile")
    if compiled:
        out["compile"] = {k: compiled.get(k) for k in ("exit", "tree_fails", "error")
                          if compiled.get(k) is not None}
        if compiled.get("error_excerpt"):
            out["compile"]["excerpt"] = str(compiled["error_excerpt"])[-1500:]
    lint = proof.get("lint")
    if lint:
        base_fails = bool(lint.get("tree_fails") or lint.get("error_kind") == "base-lint")
        out["lint"] = {"exit": lint.get("exit"), "base_fails": base_fails}
        if lint.get("error") and not base_fails:
            out["lint"]["error"] = str(lint["error"])[-1500:]
        if lint.get("error_excerpt"):
            out["lint"]["excerpt"] = str(lint["error_excerpt"])[-1500:]
    related = proof.get("related_tests")
    if related:
        out["related_tests"] = {"files": list(related.get("files") or [])[:30],
                                "exit": (related.get("run") or {}).get("exit"),
                                "base_fails": bool(related.get("base_fails"))}
    suite = proof.get("suite")
    if suite:
        out["suite"] = {k: suite.get(k) for k in ("skipped", "confirmed", "flake", "excluded",
                                                  "new_failures", "rebaselined")
                        if suite.get(k) is not None}
    return out


def _round(r: dict) -> dict:
    """A round of code review as the app shows it; `answering` may be one
    reason rather than a list of comments."""
    answering = r.get("answering")
    return {"round": r.get("round"), "ending": r.get("ending"), "detail": r.get("detail"),
            "answering": answering if isinstance(answering, list) else
            [str(answering)] if answering else [],
            "claims": r.get("claims") or [], "responses": r.get("responses") or [],
            "required_pass": issue_gates.revision_passed(r)}


def distill(record: dict, question: dict | None = None) -> dict:
    """The `fix_run` section for a finished run's `result.json` record."""
    res = record.get("result") or {}
    patches = {c.get("index"): c for c in res.get("candidate_patches") or []}
    candidates = []
    for c in res.get("candidates") or []:
        verdict = (patches.get(c.get("index")) or {}).get("verdict") or {}
        candidates.append({
            "index": c.get("index"), "model": c.get("model"), "ending": c.get("ending"),
            "detail": c.get("detail"), "reproduces": c.get("reproduces"),
            "passes": c.get("passes"), "fix_lines": c.get("fix_lines"),
            "tests": c.get("tests"), "summary": verdict.get("summary"),
            "root_cause": verdict.get("root_cause"), "changes": verdict.get("changes")})
    patch = str(res.get("patch") or "")
    reviews = [{"lens": r.get("lens"), "model": r.get("model"), "verdict": r.get("verdict"),
                "failed": r.get("failed"),
                "reason": r.get("reason"), "concerns": r.get("concerns") or [],
                "unasked": r.get("unasked") or [], "coverage": r.get("coverage") or []}
               for r in issue_gates.latest_reviews(res.get("reviews"))]
    out: dict = {
        "lane": record.get("lane"), "ending": record.get("ending"),
        "fault": bool(record.get("fault")), "detail": record.get("detail"),
        "host": settings.worker_id(), "base_sha": record.get("base_sha"),
        "report_sha": record.get("report_sha"), "models": record.get("models") or [],
        "started": record.get("started"), "finished": record.get("finished"),
        "agent_runs": record.get("agent_runs"), "summary": res.get("summary"),
        "root_cause": res.get("root_cause"), "changes": res.get("changes") or [],
        "pick": res.get("pick"), "patch": patch[:PATCH_MAX],
        "patch_truncated": len(patch) > PATCH_MAX,
        "tests": [p for p in diffpaths.changed_paths(patch) if diffpaths.is_test_path(p)],
        "agreement": res.get("agreement"), "readings": res.get("readings"),
        "tier": res.get("tier"), "proof": _proof(res.get("proof") or {}),
        "reviews": reviews, "boundary": res.get("boundary"), "intake": res.get("intake"),
        "second_opinion": res.get("second_opinion"),
        "revisions": [_round(r) for r in res.get("revisions") or []],
        "claims": [{k: c.get(k) for k in ("defect", "fix", "status", "why", "ruling", "paths",
                                          "round")}
                   for c in res.get("claims") or []],
        "candidates": candidates,
        "question": None, "proposal": None,
    }
    if question and "question" in question:
        out["question"] = {**question, "labels": dispute_question.labels(
            len(question.get("options") or []))}
    return out
