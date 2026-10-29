"""What a PR page's status block shows beyond the PR's standing
(`automation.classify`): the path through the stages to ready-to-merge, what
was tried (the lanes' outcomes, `pipeline.outcomes`), and, for a stalled PR,
the first message that asks the agent why. For a moving PR, `next_step`
says where it stands in its lane's queue and roughly when its turn comes; it
orders every hunter pool, so the page asks for it separately.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, TypedDict

from issue_triage import fix_review, followup
from pipeline import freshness, outcomes, storekit
from prospector_app.backend import (automation, data, fix_worker, issue_data, pr_checks,
                                    verify_worker)

if TYPE_CHECKING:
    from pipeline.model import Pr

StageStatus = Literal["done", "current", "failed", "todo"]


class Stage(TypedDict):
    key: str
    label: str
    status: StageStatus
    detail: str


# Each stage on the way to ready-to-merge, and the rollup checks it reads.
STAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("analysis", "Analysis", ()),
    ("threat", "Threat scan", ("secrets",)),
    ("conflicts", "Conflicts", ("mergeable", "drift")),
    ("ci", "CI", ("ci",)),
    ("review", "Reviews & scans", ("review", "scans")),
    ("design", "Design review", ("design",)),
    ("security", "Security review", ("security",)),
    ("verify", "Test run", ("verify",)),
    ("ready", "Ready to merge", ()),
)
STAGE_KEYS = tuple(key for key, _, _ in STAGES)

ASK = ("Work out why this PR is stuck and recommend one next step: re-attempt with "
       "specific notes, accept the blocker, or abandon. Say which and why. Don't take "
       "any action.")
TRIED_MAX = 5


def stages(pr: Pr, standing: dict, *, blocked_by: str | None = None) -> list[Stage]:
    """Each stage done, failed, or not yet reached, with the first one not done
    `current`. `blocked_by` names whose bar a handed-back fix PR could not
    clear (`fix_review.blocked_by`), which fails that stage."""
    checks = {c["key"]: c for c in pr_checks.checks_for_record(pr)["checks"]}
    out: list[Stage] = []
    reached = False
    for key, label, keys in STAGES:
        rows = [checks[k] for k in keys if k in checks]
        if key == "analysis":
            done, failed = freshness.is_current(pr, "analysis"), False
        elif key == "ready":
            done, failed = standing.get("state") == "ready", False
        else:
            done = bool(rows) and all(r["status"] == "pass" for r in rows)
            failed = any(r["status"] == "fail" for r in rows)
        if blocked_by is not None and key == ("ci" if blocked_by == followup.CI
                                              else "review"):
            failed = True
        detail = "; ".join(str(r.get("detail") or "") for r in rows if r.get("detail"))
        status: StageStatus = ("failed" if failed else "done" if done
                               else "todo" if reached else "current")
        reached = reached or not done
        out.append({"key": key, "label": label, "status": status, "detail": detail})
    return out


def tried(pr: Pr, limit: int = TRIED_MAX) -> list[dict]:
    """The lanes' outcomes on this PR, newest first: its own ledger rows and,
    for a Prospector fix PR, its issue's fix and follow-up rows."""
    issue = automation.fix_issue(pr)
    rows = [r.raw for r in data.runs() if isinstance(r, storekit.PhaseRun)]
    if issue is not None:
        rows += [r.raw for r in issue_data.runs() if isinstance(r, storekit.PhaseRun)]
    got: list[dict] = []
    for raw in rows:
        o = outcomes.read(raw.get("outcome"))
        if o is None:
            continue
        subject = o["subject"]
        if (subject["kind"], subject["number"]) in (
                ("pr", pr.number), ("issue", issue.number if issue else -1)):
            got.append({**o, "at": str(raw.get("finished") or raw.get("started") or "")})
    return sorted(got, key=lambda t: t["at"], reverse=True)[:limit]


def seed(pr: Pr, standing: dict, tried_: list[dict]) -> str:
    """The first message asking the agent why this PR is stuck."""
    issue = automation.fix_issue(pr)
    whose = (f"Prospector's own fix PR, for issue #{issue.number}" if issue is not None
             else "an outside contributor's PR")
    lines = [f"PR #{pr.number}: {pr.title or ''} ({whose}).",
             f"Status: {standing.get('state')} — {standing.get('reason') or ''}"]
    if tried_:
        lines.append("What was tried, newest first:")
        lines += [f"- {t['at'][:10]} {t['lane']} {t['stage']} {t['fault'] or 'ok'}: {t['words']}"
                  for t in tried_]
    lines.append(ASK)
    return "\n".join(lines)


def status(pr: Pr) -> dict:
    """The status block's data for one open PR."""
    standing = automation.classify(pr) or {}
    issue = automation.fix_issue(pr)
    by = None
    if issue is not None and standing.get("bucket") == "handed-back":
        fu = fix_review.followup_for(issue.fix_followup, pr.number)
        by = fix_review.blocked_by(fu.get("judged") or {})
    tried_ = tried(pr)
    return {"standing": standing, "stages": stages(pr, standing, blocked_by=by),
            "tried": tried_, "issue": issue.number if issue is not None else None,
            "seed": seed(pr, standing, tried_) if standing.get("state") == "stalled" else None}


# Each hunter lane's ledger phase, whose finished runs measure its pace.
LANE_PHASES: dict[str, str] = {"security": "security:review-one", "verify": "verify:single",
                               "design": "design-review:run", "fix": "fix:single"}


class NextStep(TypedDict):
    lane: str
    position: int
    of: int
    eta_hours: float | None


def lane_rate(lane: str, *, days: int = 7) -> float | None:
    """Finished runs of `lane` per hour over the last `days`, or None when
    there were none."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    count = sum(1 for r in data.runs(since=since)
                if isinstance(r, storekit.PhaseRun) and r.phase == LANE_PHASES[lane])
    return count / (days * 24) if count else None


def next_step(n: int, *, days: int = 7) -> NextStep | None:
    """Where PR `n` stands in the first hunter queue that holds it, in the
    order the hunters pick, and roughly how many hours until its turn at the
    lane's recent pace. None when no queue holds it."""
    limit = max(len(data.prs()), 1000)
    order: dict[str, list[int]] = dict(verify_worker.hunt_candidates(limit))
    order["fix"] = [pr for _, pr in fix_worker.hunt_candidates(limit)]
    for lane in ("security", "verify", "design", "fix"):
        queue = order.get(lane) or []
        if n in queue:
            position = queue.index(n) + 1
            rate = lane_rate(lane, days=days)
            return {"lane": lane, "position": position, "of": len(queue),
                    "eta_hours": round(position / rate, 1) if rate else None}
    return None
