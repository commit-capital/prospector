"""Where each open PR stands with the automation, derived on read.

One classification per PR, computed from the same gates and hunter predicates
the workers decide by, into three columns: `act` (a click of yours moves it),
`auto` (a worker queue or hunter moves it, nothing for a person to do), and
`handed` (the automation gave it back — to its author, or to you). The
`bucket` names the situation inside the column and `owner` says whose move it
is; `reason` is the one-line why. Home counts, the Explorer's
`automation_*` filters, and every card link read this one derivation, so the
number on a card is the row count its link opens. Pure: no network, no store
writes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline import describe_pr, freshness, gates, settings

if TYPE_CHECKING:
    from pipeline.model import Pr

COLUMNS = ("act", "auto", "handed")

# bucket -> owner. `you` is the operator, `author` the contributor, `worker`
# the automation itself.
OWNERS: dict[str, str] = {
    "merge-ready": "you", "approve-parked": "you",
    "queued": "worker", "hunt": "worker", "waiting": "worker", "retry": "worker",
    "budgeted": "worker",
    "author-conflicts": "author", "author-ci": "author", "author-declined": "author",
    "author-verify": "author", "author-rejected": "author", "author-other": "author",
    "needs-human": "you", "security-red": "you", "security-yellow": "you", "asks": "you",
    "gated": "you", "other": "you",
}

AUTHOR_BUCKETS = tuple(b for b, o in OWNERS.items() if o == "author")
YOU_HANDED_BUCKETS = ("needs-human", "security-red", "security-yellow", "asks", "gated", "other")

_VERIFY_QUEUE = ("queued", "running", "waiting-for-base")


def _r(column: str, bucket: str, reason: str) -> dict:
    return {"column": column, "bucket": bucket, "owner": OWNERS[bucket], "reason": reason[:300]}


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
    from prospector_app.backend import fix_queue, fix_worker, service
    req = pr.fix_request or {}
    ok, _why = gates.merge_eligibility(pr)
    if ok and pr.disposition == "merge":
        return _r("act", "merge-ready", "every gate is clear; merge it")
    if req.get("status") == "awaiting-review":
        return _r("act", "approve-parked",
                  f"a parked {req.get('action')} awaits your approval")
    if pr.disposition == "needs-human":
        return _r("handed", "needs-human", pr.rationale or "flagged needs-human")
    if pr.security_verdict == "RED" and freshness.is_current(
            pr, "security", max_age_days=gates.SECURITY_MAX_AGE_DAYS):
        return _r("handed", "security-red", "the security review returned RED")
    if req.get("status") in fix_queue.IN_FLIGHT:
        return _r("auto", "queued", f"in the fix queue ({req.get('status')})")
    vr = pr.section("verify_request") or {}
    if vr.get("status") in _VERIFY_QUEUE:
        return _r("auto", "queued", f"in the verify queue ({vr.get('status')})")

    if pr.mergeable is False:
        action = "rebase"
    elif pr.drift_state == "conflicts":
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
    return _blocked(pr, action, why)


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
    if "stale or pending" in low or "awaiting" in low or "review stale" in low:
        return _r("auto", "waiting", "waiting on a reviewer verdict for this head; "
                                     "the worker asks for it")
    if "signals or reviews stale" in low:
        return _r("auto", "waiting", "waiting on re-ingest")
    if "ci is pending" in low:
        return _r("auto", "waiting", "CI is running")
    if "ci is failing" in low:
        return _r("handed", "author-ci", "CI fails at the author's head")
    if "codeowners" in low or "withholds" in low:
        return _r("handed", "gated", why)
    if "malicious" in low or "threat" in low or "returned red" in low:
        return _r("handed", "security-red", why)
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
    if action in ("rebase", "update"):
        return _r("handed", "author-conflicts",
                  f"needs a rebase, and the review bar is not met ({why}), so the hunter "
                  f"leaves it to the author")
    return _r("handed", "other", why)
