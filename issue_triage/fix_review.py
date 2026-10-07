"""The ONE policy for reviewing issue fixes in the app: what an operator may ask
of an issue's fix attempt, the status the attempt is in, and the attempt as the
app shows it.

An issue holds at most one pending request (`fix_request`, actions
`issue_store.ISSUE_FIX_ACTIONS`), which `queue` admits only when it fits the
latest attempt (`fix_run`) and no other request is in flight; the words an
operator gives with it land in the thread (`fix_thread`). `fix_status` derives,
on read, whose move the attempt waits on; a proposal's follow-up record
(`fix_followup`) counts only while it follows the run's own pull request.
`distill` turns a finished run's `result.json` record into the `fix_run` the app
renders: each candidate's own account, the agreement, the picked change, the
checks, the reviewers, and the question — never an agent's transcript.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from issue_triage import dispute_question
from pipeline import diffpaths, gates, settings, storekit

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from issue_triage.issue_store import IssueStore

IN_FLIGHT = ("queued", "running")
FAULT_ENDINGS = ("agent-unavailable", "run-failed", "sandbox", "base-compile")
# The largest picked change `fix_run` carries; a longer one is cut and flagged.
PATCH_MAX = 60_000
# The status of an issue's fix attempt, in the order the explorer groups them.
STATUSES = ("review", "question", "running", "reporter", "pr-open", "pr-closed", "failed",
            "declined", "pr-merged")


def followup_for(fu: dict | None, pr: object) -> dict:
    """A copy of the follow-up record `fu` when it follows pull request `pr`,
    else an empty one."""
    if not fu or pr is None or str(fu.get("pr")) != str(pr):
        return {}
    return dict(fu)


def _ended(proposal: dict, fu: dict) -> tuple[str, str] | None:
    """(status, reason) for a proposal whose follow-up saw it end, or None
    while it is open. Only `closed_as: merged` reads as merged; a record
    without `closed_as` reads as closed, in the follow-up's own words."""
    if fu.get("state") != "done":
        return None
    pr = proposal["pr"]
    if fu.get("closed_as") == "merged":
        return "pr-merged", f"#{pr} merged"
    if fu.get("closed_as") == "closed":
        return "pr-closed", f"#{pr} was closed without merging"
    return "pr-closed", str(fu.get("reason") or f"#{pr} is no longer open")


def fix_status(issue: Issue) -> tuple[str, str] | None:
    """(status, reason) for issue's fix attempt, or None when it has none."""
    req, run = issue.fix_request, issue.fix_run
    if req and req.get("status") in IN_FLIGHT:
        return "running", f"{req.get('action')} {req.get('status')}"
    if req and req.get("status") == "failed":
        return "failed", str(req.get("reason") or f"{req.get('action')} failed")
    if not run:
        return None
    proposal = run.get("proposal") or {}
    if proposal.get("pr"):
        fu = followup_for(issue.fix_followup, proposal["pr"])
        ended = _ended(proposal, fu)
        if ended:
            return ended
        if fu.get("state") == "ready":
            return "review", f"#{proposal['pr']} is green — ready for your review"
        if fu.get("state") == "handed-back":
            return "review", f"#{proposal['pr']} handed back: {fu.get('reason') or ''}"
        if fu.get("state") == "watching" and fu.get("reason"):
            return "pr-open", f"#{proposal['pr']}: {fu['reason']}"
        return "pr-open", f"#{proposal['pr']}"
    ending, detail = run.get("ending"), str(run.get("detail") or "")
    if ending == "fixed":
        return "review", detail
    if ending == "fix-disputed":
        question = run.get("question") or {}
        if question.get("asked"):
            return "reporter", "a question is waiting on the reporter"
        if question.get("question"):
            return "question", str(question["question"])
        return "declined", "the agents disagreed and no question could be drafted"
    if ending in FAULT_ENDINGS or run.get("fault"):
        return "failed", f"{ending}: {detail}"
    return "declined", f"{ending}: {detail}"


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
          dry_run: bool = False) -> tuple[bool, str]:
    """Queue `action` on issue `n`'s fix attempt for the worker, recording the
    words it carries in the thread. `guidance` is an operator's or maintainer's
    instruction; `notes` are words from anyone else — the issue's author, the
    code reviewers, CI — which the fix agent weighs as data. `rivals` names other
    authors' pull requests whose tests a `send-back` judges the fix by. A request that
    retries a failed one of the same action carries its attempt count on.
    (ok, reason)."""
    issue = store.load_issue(n)
    if issue is None:
        return False, f"issue #{n} is not in the store"
    if (issue.raw.get("meta") or {}).get("state") != "open":
        return False, f"issue #{n} is closed"
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
    issue.record_fix_request(section)
    given = answer or {}
    words = "\n\n".join(w for w in ((guidance or "").strip(), str(given.get("text") or "").strip(),
                                    (notes or "").strip()) if w)
    if given.get("label"):
        words = f"Answered {given['label']}" + (f": {words}" if words else "")
    if words:
        issue.append_fix_thread({"at": section["queued_at"], "by": by,
                                 "kind": "answer" if action == "answer" else "comment",
                                 "action": action, "text": words})
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
    """Issues with a queued request, a maintainer's (gates.priority_author)
    first, then oldest first."""
    rows = [(not gates.priority_author(i.author, i.author_association),
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
    reviews = [{"lens": r.get("lens"), "verdict": r.get("verdict"), "failed": r.get("failed"),
                "reason": r.get("reason"), "concerns": r.get("concerns") or [],
                "unasked": r.get("unasked") or []} for r in res.get("reviews") or []]
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
        "candidates": candidates,
        "question": None, "proposal": None,
    }
    if question and "question" in question:
        out["question"] = {**question, "labels": dispute_question.labels(
            len(question.get("options") or []))}
    return out
