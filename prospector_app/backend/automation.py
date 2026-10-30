"""Where each open PR stands with the automation, derived on read.

One classification per PR, computed from the same gates and hunter predicates
the workers decide by, into three columns: `act` (a click of yours moves it),
`auto` (a worker queue or hunter moves it, nothing for a person to do), and
`handed` (the automation gave it back — to its author, or to you). The
`bucket` names the situation inside the column, `owner` says whose move it
is, `state` is the status a PR's page and the Explorer lead with (STATES), and
`reason` is the one-line why. Home counts, the Explorer's
`automation_*` filters, and every card link read this one derivation, so the
number on a card is the row count its link opens. Pure: no network, no store
writes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline import describe_pr, design_comment, freshness, gates, settings

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from pipeline.model import Pr

COLUMNS = ("act", "auto", "handed")

# bucket -> owner. `you` is the operator, `author` the contributor, `worker`
# the automation itself.
OWNERS: dict[str, str] = {
    "malicious": "you", "merge-ready": "you", "approve-parked": "you", "approve-ci": "you",
    "rerun-ci": "you",
    "queued": "worker", "hunt": "worker", "waiting": "worker", "retry": "worker",
    "budgeted": "worker",
    "author-conflicts": "author", "author-ci": "author", "author-declined": "author",
    "author-verify": "author", "author-rejected": "author", "author-design": "author",
    "author-other": "author",
    "stacked": "author",
    "needs-human": "you", "security-red": "you", "security-yellow": "you", "asks": "you",
    "gated": "you", "other": "you",
    "followup": "worker", "handed-back": "you", "answered": "you",
}

# bucket -> the state a PR's status leads with: `moving` (a worker moves it),
# `waiting` (someone else's move), `ready` (green; merge it), `needs-you` (one
# quick click of yours, nothing broken) and `stalled` (the automation gave up
# or got stuck).
_NEEDS_YOU = ("malicious", "approve-parked", "approve-ci", "rerun-ci", "answered")
STATES: dict[str, str] = {
    b: ("ready" if b == "merge-ready" else "needs-you" if b in _NEEDS_YOU
        else {"worker": "moving", "author": "waiting"}.get(o, "stalled"))
    for b, o in OWNERS.items()}

AUTHOR_BUCKETS = tuple(b for b, o in OWNERS.items() if o == "author")
YOU_HANDED_BUCKETS = ("needs-human", "security-red", "security-yellow", "asks", "gated", "other",
                      "handed-back")

_VERIFY_QUEUE = ("queued", "running", "waiting-for-base")


_ANSWERED = {"design": "design findings", "review": "review score", "ci": "failing CI"}


def _r(column: str, bucket: str, reason: str) -> dict:
    return {"column": column, "bucket": bucket, "owner": OWNERS[bucket], "state": STATES[bucket],
            "reason": reason[:800]}


def bot_broke_ci(pr: Pr) -> bool:
    """Whether the PR's current head is one this bot pushed and CI fails at it."""
    req = pr.fix_request or {}
    pushed = str((req.get("result") or {}).get("pushed_head_sha") or "")
    return (req.get("status") == "pushed" and bool(pushed) and pushed == pr.head_sha
            and pr.ci == "failing" and freshness.is_current(pr, "signals"))


def classify(pr: Pr) -> dict | None:
    """The PR's standing with the automation, or None for a PR that is not
    open."""
    if pr.state != "open":
        return None
    if pr.threat_verdict == "malicious":
        return _r("act", "malicious",
                  "the threat scan flagged it malicious ("
                  + (", ".join(pr.threat_signatures) or "flagged")
                  + "); it can never merge — close it")
    if pr.stacked:
        from prospector_app.backend import service
        base = service.stacked_on(pr)
        on = f"#{base} ({pr.base_ref})" if base is not None else f"branch {pr.base_ref}"
        return _r("handed", "stacked", f"stacked on {on} — waits for its base to merge "
                                       f"and GitHub to retarget it onto {settings.default_branch()}")
    if (ours := _followup_standing(pr)) is not None:
        return ours
    if gates.bump_exempt(pr):
        return _r("handed", "other", "a dependency bump, outside the automation's scope; "
                                     "merge it once CI and the scanners pass")
    from prospector_app.backend import fix_queue, fix_worker, service
    req = pr.fix_request or {}
    if (merge := _merge_standing(pr)) is not None:
        return merge
    if req.get("status") == "awaiting-review":
        return _r("act", "approve-parked", _parked_reason(req))
    if pr.disposition == "merge" and gates.pr_clean(pr)[1] == [gates.THREAT_SCAN_STALE]:
        return _r("auto", "waiting", "the threat scan has not judged this head yet; "
                                     "a worker scans new heads every ten minutes")
    if pr.disposition == "needs-human":
        return _r("handed", "needs-human", pr.rationale or "flagged needs-human")
    if pr.security_verdict == "RED" and freshness.is_current(pr, "security"):
        return _r("handed", "security-red", "the security review returned RED")
    if pr.disposition in ("merge", None) and gates.blocked_on_security(pr):
        if pr.section("security") and pr.security_verdict != "GREEN":
            return _r("auto", "waiting", f"the security review was {pr.security_verdict} at "
                                         "an earlier head; the security hunter reviews this one")
        return _r("auto", "waiting", "the deep security review has not run on this head; "
                                     "the security hunter picks it up")
    unscannable = gates.unscannable_reason(pr)
    if unscannable and pr.threat_verdict != "malicious":
        return _r("handed", "other", unscannable)
    if req.get("status") in fix_queue.IN_FLIGHT:
        return _r("auto", "queued", f"in the fix queue ({req.get('status')})")
    vr = pr.section("verify_request") or {}
    if vr.get("status") in _VERIFY_QUEUE:
        return _r("auto", "queued", f"in the verify queue ({vr.get('status')})")
    if pr.ci == "awaiting-approval" and freshness.is_current(pr, "signals"):
        return _r("act", "approve-ci", gates.CI_AWAITING_APPROVAL)
    if pr.ci_stale_checks and freshness.is_current(pr, "signals"):
        return _r("act", "rerun-ci", gates.ci_stale_reason(pr.ci_stale_checks))

    if (answer := gates.author_answer(pr)) is not None:
        then = ("approve the design review or hand it back" if answer.check == "design"
                else "hand it back once you've read it")
        return {**_r("act", "answered", f"💬 “{answer.text}”: the author's answer to the "
                                        f"{_ANSWERED[answer.check]}; {then}"),
                "answer": answer.check}
    if pr.mergeable is False:
        action = "rebase"
    elif pr.drift_state == "conflicts" or pr.ci == "unreported":
        action = "update"
    else:
        action = "describe" if describe_pr.only_description_nits(pr) else "fix"
    objection: str | bool = False
    if action in ("fix", "describe") and settings.fix_hunt_fix():
        if bot_broke_ci(pr):
            action, objection = "fix", "ci"
        elif settings.fix_hunt_security() and fix_worker._yellow_objection(pr) is not None:
            action, objection = "fix", "security"
    if fix_worker._hunt_attempted(pr, action):
        return _rested(pr, req, action)
    ok, why = gates.fix_huntable(pr, action, service.changed_paths(pr), objection=objection)
    if ok:
        what = {"ci": "a fix for the CI this bot broke",
                "security": "a fix from the security finding"}.get(str(objection), action)
        if objection == "security":
            return _r("auto", "budgeted", "the hunter's next pick, behind the day's "
                                          "continuation budget: " + what)
        return _r("auto", "hunt", f"the hunter's next pick: {what}")
    if design_comment.posted(pr) is not None:
        return _r("handed", "author-design", "waiting on the author to address the design "
                                             "findings posted on the PR")
    return _blocked(pr, action, why)


def _merge_standing(pr: Pr) -> dict | None:
    """A merge pick's standing once the human merge gate passes: ready, gated
    on a code owner, or the check still missing. None when the gate refuses
    or the PR is no merge pick. A PR ANALYZE never reached is mergeable on the
    human gate alone."""
    from prospector_app.backend import service
    ok, why = gates.merge_eligibility(pr)
    if not ok or pr.disposition not in ("merge", None):
        return None
    # The cached diff's paths, when there is one: a CODEOWNERS path is a code
    # owner's merge, never the bot's.
    paths = service.changed_paths(pr)
    if paths:
        ok, why = gates.merge_eligibility(pr, changed_paths=paths)
        if not ok:
            return _r("handed", "gated", why)
    if pr.disposition is None:
        # Not ready: the verify hunter runs only analyzed merge picks.
        return _r("auto", "waiting", "not analyzed yet; once the clustering lane picks "
                                     "it to merge, the verify hunter runs its Test Run")
    gap = _unclear_check(pr)
    if gap is None:
        return _r("act", "merge-ready", "every check is clear; merge it")
    return _merge_pick_gap(pr, gap)


def fix_issue(pr: Pr) -> Issue | None:
    """The open issue a Prospector fix PR was proposed for, or None for anyone
    else's PR or one whose issue the snapshot does not hold."""
    if not gates.opened_by_bot(pr):
        return None
    from issue_triage import fix_review
    from prospector_app.backend import issue_data
    return next((i for i in issue_data.issues().values()
                 if i.state == "open" and fix_review.open_pr(i) == pr.number), None)


def _followup_standing(pr: Pr) -> dict | None:
    """A Prospector fix PR's standing: handed back to you, or, once green or
    approved, wherever the merge gates place it like any other PR, and
    otherwise followed up by the issue-fix lane, which answers its reviewers,
    CI and conflicts itself. None for anyone else's PR, or one whose issue the
    snapshot does not hold."""
    issue = fix_issue(pr)
    if issue is None:
        return None
    from issue_triage import fix_review
    fu = fix_review.followup_for(issue.fix_followup, pr.number)
    line = fix_review.status_line(issue)["text"]
    if fu.get("state") == "handed-back" and fix_review.approval(issue) is None:
        return _r("handed", "handed-back", line)
    if fu.get("state") == "ready" or fix_review.approval(issue) is not None:
        if (merge := _merge_standing(pr)) is not None:
            return merge
    return _r("auto", "followup", line)


def _parked_reason(req: dict) -> str:
    """Why a parked change waits for you: the unattended bar that stopped it,
    or the parking worker not pushing that action unattended."""
    action = str(req.get("action"))
    base = settings.default_branch()
    what = {"resolve": "the agent's conflict resolve", "fix": "the agent's fix",
            "describe": "the rewritten description", "update": f"the update from {base}",
            "rebase": f"the rebase onto {base}"}.get(action, f"the {action}")
    result = req.get("result") or {}
    bar = (result.get("auto_review") or {}).get("bar") or result.get("autopush_bar") or {}
    if bar and not bar.get("ok"):
        why = bar.get("reason") or "the unattended bar refused it"
    elif result.get("autopush") is False:
        why = f"unattended pushes are off for {action} on {req.get('host') or 'its worker'}"
    elif result.get("autopush"):
        why = "the unattended bar has not judged it yet"
    else:
        return f"{what} awaits your approval"
    return f"{what} is parked for you: {why}"


def _unclear_check(pr: Pr) -> dict | None:
    """The first rollup check that is not clear on a merge-eligible pick, or
    None. A failed or warning check is not clear, except "Includes tests",
    which may warn (a PR without tests can still be ready), and an advisory
    check, which never gates. A check that has
    not run counts against readiness only for verification and the deep
    security review, the two the hunters exist to run; a check that does not
    apply to this deployment (no active scanner) is clear."""
    from prospector_app.backend import pr_checks
    for check in pr_checks.checks_for_record(pr)["checks"]:
        status, key = check["status"], check["key"]
        if status == "pass" or check["advisory"] or (key == "tests" and status == "warn"):
            continue
        if status == "na" and key not in ("verify", "security"):
            continue
        return check
    return None


def _merge_pick_gap(pr: Pr, check: dict) -> dict:
    """A merge pick the human gate would pass but a check has not cleared:
    what fills the gap, and whose it is."""
    key, name, detail = check["key"], check["name"], str(check.get("detail") or "")
    if key == "verify":
        if check["status"] == "warn" and pr.verify_outcome is not None:
            return _r("handed", "other", f"verification is partial evidence ({detail}); merge on "
                                          f"your judgment or ask for an independent repro")
        vr = pr.section("verify_request") or {}
        if vr.get("status") in _VERIFY_QUEUE:
            return _r("auto", "queued", f"in the verify queue ({vr.get('status')})")
        if vr.get("status") == "error":
            final = gates.verify_retry_exhausted(pr)
            if final:
                return _r("handed", "other", f"verification will not re-run on its own ({final}); "
                                              f"re-queue it or merge on your own judgment "
                                              f"({vr.get('error') or 'no error recorded'})")
            return _r("auto", "waiting", "verification hit a machine fault, not the PR; "
                                         "it re-runs automatically")
        return _r("auto", "waiting", "verification has not run yet; the verify hunter picks it up")
    if key == "security":
        return _r("auto", "waiting", "the deep security review has not run yet; the security "
                                     "hunter picks it up")
    return _r("auto", "waiting", f"{name}: {detail or 'not clear yet'}")


def _rested(pr: Pr, req: dict, action: str) -> dict:
    """A head the hunter already acted on at `action`: where its ending left it."""
    status = req.get("status")
    if status == "pushed":
        return _r("auto", "waiting", "the bot pushed; waiting on the reviewer and CI")
    if status == "failed":
        return _r("auto", "retry", "a machine failure; the hunter retries after its cooldown")
    text = str(req.get("refused_reason") or (req.get("result") or {}).get("detail")
               or req.get("error") or "")
    low = text.lower()
    if "declined to write" in low or "declined:" in low:
        return _r("handed", "author-declined", text)
    if "reviewing agent rejected" in low:
        return _r("handed", "author-rejected", text)
    if "codeowners" in low or "withholds" in low:
        return _r("handed", "gated", text)
    if "safeguards" in low:
        return _r("handed", "other", text)
    if ("same lines" in low or "conflict" in low or "no longer applies" in low
            or "needs a rebase" in low or "rebase couldn't" in low):
        return _r("handed", "author-conflicts", text)
    if status == "cancelled":
        return _r("handed", "other", text or "cancelled at this head")
    return _r("handed", "author-other", text or f"the hunter refused the {action}")


def _blocked(pr: Pr, action: str, why: str) -> dict:
    """A head the hunter will not act on: what stands in the way, and whose."""
    low = why.lower()
    if "signals or reviews stale" in low:
        return _r("auto", "waiting", "waiting on re-ingest")
    if action == "rebase" or (action == "update" and pr.drift_state == "conflicts"):
        # The hunter rebases only at the review bar, so a conflicted PR below
        # it waits on its author. The re-review hunter asks for a missing
        # verdict on a recently updated conflicted head.
        return _r("handed", "author-conflicts",
                  f"needs a rebase, and the hunter leaves it to the author: {why}")
    if "stale or pending" in low or "awaiting" in low or "review stale" in low:
        return _r("auto", "waiting", "waiting on a reviewer verdict for this head; "
                                     "the worker asks for it")
    if "ci is pending" in low:
        return _r("auto", "waiting", "CI is running")
    if "ci is unreported" in low:
        return _r("handed", "author-ci", gates.CI_UNREPORTED)
    if "ci is failing" in low:
        return _r("handed", "author-ci", "CI fails at the author's head")
    if "codeowners" in low or "withholds" in low:
        return _r("handed", "gated", why)
    if "returned red" in low:
        return _r("handed", "security-red", why)
    if action == "update":
        return _r("handed", "author-ci", f"{gates.CI_UNREPORTED}; the hunter will not: {why}")
    if "no gate a fix could clear" in low:
        dem = gates.merge_demotion(pr)
        if dem is not None:
            reason = str(dem[1] or "")
            if "verification" in reason.lower():
                return _r("handed", "author-verify", reason)
            if "yellow" in reason.lower():
                if settings.fix_hunt_fix() and settings.fix_hunt_security():
                    return _r("auto", "budgeted", "a YELLOW finding the hunter fixes as an "
                                                  "objection, behind the day's budget")
                return _r("handed", "security-yellow", reason)
            if "draft" in reason.lower():
                return _r("handed", "author-other", reason)
            return _r("handed", "asks", reason or "the analysis asks for changes")
        if pr.disposition == "merge":
            _ok, missing = gates.merge_eligibility(pr)
            return _r("auto", "waiting", f"a merge pick the verify and security hunters "
                                         f"finish: {missing}")
        asks = pr.asks or []
        return _r("handed", "asks", str(asks[0]) if asks else (pr.rationale or why))
    return _r("handed", "other", why)
