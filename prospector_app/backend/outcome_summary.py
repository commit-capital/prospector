"""Where the lanes' attempts stopped over a window, for the Results page:
attempts grouped by lane and stage with each one's own words, the same stop
repeating on one subject, whose fault each week's stops were, and the fixes
parked for an operator now.

It reads the issue ledger and issue snapshot `issue_data` already holds in
memory, so a request costs one pass over the window's rows and the open issues
with a fix attempt.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from issue_triage import fix_outcome, fix_review, trust_boundary
from pipeline import storekit
from pipeline.outcomes import Asked
from prospector_app.backend import issue_data

CASES_MAX = 40
SUCCESS = ("fixed", "reproduced")


class Case(TypedDict):
    issue: int | None
    pr: int | None
    at: str
    trigger: str
    by: str | None
    words: str
    asked: Asked | None
    leaves: list[str]


class Group(TypedDict):
    lane: str
    stage: str
    fault: str | None
    count: int
    cases: list[Case]


class Repeat(TypedDict):
    lane: str
    stage: str
    kind: str
    number: int
    issue: int | None
    count: int
    last: str


class Week(TypedDict):
    week: str
    ok: int
    machine: int
    subject: int
    judged: int


class ParkedCase(TypedDict):
    issue: int
    title: str | None
    why: str


class Parked(TypedDict):
    kind: str
    count: int
    cases: list[ParkedCase]


class Summary(TypedDict):
    days: int
    since: str
    attempts: int
    groups: list[Group]
    repeats: list[Repeat]
    weeks: list[Week]
    parked: list[Parked]


def _at(row: dict) -> str:
    return str(row.get("finished") or row.get("started") or "")


def _week(at: str) -> str:
    day = datetime.fromisoformat(at).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def summarize(rows: list[dict], *, since: str) -> tuple[int, list[Group], list[Repeat], list[Week]]:
    """The window's attempts from ledger `rows`, newest first within each group."""
    groups: dict[tuple[str, str], Group] = {}
    repeats: Counter[tuple[str, str, str, int]] = Counter()
    last: dict[tuple[str, str, str, int], tuple[str, int | None]] = {}
    weeks: dict[str, Counter[str]] = defaultdict(Counter)
    attempts = 0
    for raw in sorted(rows, key=_at, reverse=True):
        at = _at(raw)
        if at < since:
            continue
        got = fix_outcome.of_row(raw)
        if got is None:
            continue
        attempts += 1
        issue = raw.get("issue") if isinstance(raw.get("issue"), int) else None
        weeks[_week(at)][got["fault"] or "ok"] += 1
        leaves = [str(d) for d in (raw.get("stats") or {}).get("leaves") or []]
        if got["stage"] in SUCCESS and not leaves:
            continue
        if got["stage"] not in SUCCESS:
            key = (got["lane"], got["stage"], got["subject"]["kind"], got["subject"]["number"])
            repeats[key] += 1
            last.setdefault(key, (at, issue))
        group = groups.setdefault((got["lane"], got["stage"]), {
            "lane": got["lane"], "stage": got["stage"], "fault": got["fault"], "count": 0,
            "cases": []})
        group["count"] += 1
        if len(group["cases"]) < CASES_MAX:
            group["cases"].append({
                "issue": issue, "pr": raw.get("pr") if isinstance(raw.get("pr"), int) else None,
                "at": at, "trigger": str(raw.get("trigger") or ""), "by": got["by"],
                "words": got["words"], "asked": got["asked"], "leaves": leaves})
    return (attempts, sorted(groups.values(), key=lambda g: -g["count"]),
            [{"lane": k[0], "stage": k[1], "kind": k[2], "number": k[3], "issue": last[k][1],
              "count": c, "last": last[k][0]} for k, c in repeats.most_common() if c > 1],
            [{"week": w, "ok": c["ok"], "machine": c["machine"], "subject": c["subject"],
              "judged": c["judged"]} for w, c in sorted(weeks.items())])


def parked() -> list[Parked]:
    """Open issues whose fix waits on an operator, by why it is held."""
    out: dict[str, list[ParkedCase]] = defaultdict(list)
    for n, issue in issue_data.issues().items():
        if issue.state != "open" or not issue.fix_run:
            continue
        status = fix_review.fix_status(issue)
        if not status or status[0] != "review":
            continue
        held = trust_boundary.held_kind(issue)
        handed = (issue.fix_followup or {}).get("state") == "handed-back"
        kind = (held[0] if held else "handed-back" if handed
                else "partial" if fix_review.partial(issue) else "ready")
        out[kind].append({"issue": n, "title": issue.title, "why": held[1] if held else status[1]})
    return sorted(({"kind": k, "count": len(v), "cases": v} for k, v in out.items()),
                  key=lambda p: -p["count"])


def summary(days: int) -> Summary:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = [r.raw for r in issue_data.runs() if isinstance(r, storekit.PhaseRun)
            and r.phase in (fix_outcome.RUN_PHASE, fix_outcome.HANDBACK_PHASE)]
    attempts, groups, repeats, weeks = summarize(rows, since=since)
    return {"days": days, "since": since, "attempts": attempts, "groups": groups,
            "repeats": repeats, "weeks": weeks, "parked": parked()}
