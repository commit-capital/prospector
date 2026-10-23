"""Label suggestions for recorded reviewer runs, and each reviewer
configuration's scorecard, derived on read (docs/superpowers/specs/
2026-10-23-reviewer-eval-ratchet-design.md: Labels, Scorecard).

`gather` reads the runs of the last SIGNAL_WINDOW_DAYS and the outcomes on
their PRs and issues in at most five queries. `signals` turns those into at
most one suggestion per unlabeled run: what the reviewer probably should have
said, and the outcome that says so. A suggestion is only that; an operator
confirms it on the subject's page (verdict_labels).

`scorecard` counts, per reviewer configuration (prompt and model), how its
labeled runs fared: must-catch cases (labeled at or above the reviewer's
blocking verdict) it blocked, should-pass cases it blocked anyway, and its
under- and over-calls; and, over the window, how often it failed to judge.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from pipeline import storekit, verdict_labels, verdict_log
from pipeline.store import Store
from pipeline.storekit import VerdictRecord
from pipeline.verdict_labels import MIN_CASES
from prospector_app.backend import activity, data, issue_data

SIGNAL_WINDOW_DAYS = 60
QUOTE_MAX = 300
# Second-opinion verdicts saying a rival's tests catch what our fix misses.
GAPS = ("gap", "gap-open")
_ISSUE_PATHS: list[tuple[str, ...]] = [
    ("fix_followup",), ("fix_run", "proposal"), ("fix_run", "second_opinion"), ("fix_choice",),
    ("fix_scan",), ("fix_thread", "entries")]
_ISSUE_KEYS = ("fix_followup", "fix_run_proposal", "second_opinion", "fix_choice", "fix_scan",
               "fix_thread")


@dataclass(frozen=True)
class Facts:
    rows: list[VerdictRecord]
    prs: dict[int, dict]
    pr_events: dict[int, list[dict]]
    issues: dict[int, dict]
    fix_runs: list[dict]


@dataclass(frozen=True)
class Signal:
    verdict_id: int
    reviewer: str
    subject_kind: str
    subject: int
    verdict: str
    suggests: str
    why: str
    key: str


@dataclass(frozen=True)
class Card:
    reviewer: str
    prompt_sha: str
    model: str
    labeled: int = 0
    runs: int = 0
    failed: int = 0
    must_catch: int = 0
    caught: int = 0
    should_pass: int = 0
    blocked: int = 0
    under: int = 0
    over: int = 0


def window_start(now: datetime | None = None) -> str:
    start = (now or datetime.now(timezone.utc)) - timedelta(days=SIGNAL_WINDOW_DAYS)
    return start.isoformat(timespec="seconds")


def gather(since: str) -> Facts:
    st = data.store()
    rows = st.review_verdicts(since=since)
    prs = sorted({r.subject for r in rows if r.subject_kind == "pr"})
    issues = sorted({r.subject for r in rows if r.subject_kind == "issue"})
    events: dict[int, list[dict]] = {}
    for ev in activity.events_for_prs(prs) if prs else []:
        events.setdefault(int(ev["pr"]), []).append(ev)
    return Facts(
        rows=rows,
        prs={r["pr"]: r for r, _ in st.pr_rows([], numbers=prs)} if prs else {},
        pr_events=events,
        issues={r["issue"]: {"state": r["state"], "state_reason": r["state_reason"],
                             **dict(zip(_ISSUE_KEYS, vals, strict=True))}
                for r, vals in issue_data.store().issue_rows(_ISSUE_PATHS, numbers=issues)}
        if issues else {},
        fix_runs=_fix_runs(st, rows))


def _fix_runs(st: Store, rows: list[VerdictRecord]) -> list[dict]:
    """The PR fix-request endings a `guidance-after-pass` signal can match: on
    the PRs with a design pass in `rows`, from the earliest one on."""
    passed = [r for r in rows if r.reviewer == "design" and r.verdict == "pass"]
    if not passed:
        return []
    return [run.raw for run in st.runs(since=min(r.at for r in passed), phase="fix:single",
                                       prs=sorted({r.subject for r in passed}))
            if isinstance(run, storekit.PhaseRun)]


def _after(at: str | None, row: VerdictRecord) -> bool:
    when, then = storekit.parse_ts(at), storekit.parse_ts(row.at)
    return bool(when and then and when > then)


def _quoted(text: object) -> str:
    return f': "{str(text)[:QUOTE_MAX]}"' if text else ""


def _landed_merges(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("kind") == "merge" and activity.is_landed(e)]


def _pr_signal(row: VerdictRecord, f: Facts) -> tuple[str, str, str] | None:
    merges = _landed_merges(f.pr_events.get(row.subject, []))
    if row.reviewer == "security" and row.verdict == "YELLOW" and merges:
        # Only a merge's `started` event names the head it merged.
        for e in f.pr_events.get(row.subject, []):
            if (e.get("kind") == "merge" and e.get("status") == "started"
                    and e.get("head_sha") == row.head_sha and e.get("security_override")):
                return ("merged-over-yellow", "GREEN",
                        "An operator merged over this YELLOW" + _quoted(e["security_override"]))
    blocked = (row.reviewer == "security" and row.verdict in ("RED", "YELLOW")) or (
        row.reviewer == "design" and row.verdict == "fail")
    if blocked and (f.prs.get(row.subject) or {}).get("state") == "merged" and not merges:
        return ("merged-upstream", "GREEN" if row.reviewer == "security" else "pass",
                "Maintainers merged this PR without Prospector; check it was not reverted")
    if row.reviewer == "design" and row.verdict == "pass":
        for run in f.fix_runs:
            stats = run.get("stats") or {}
            if (run.get("pr") == row.subject and run.get("trigger") is None
                    and stats.get("guidance") and stats.get("head_sha") == row.head_sha):
                return ("guidance-after-pass", "fail",
                        "An operator sent this head back with guidance" + _quoted(stats["guidance"]))
    return None


def _issue_signal(row: VerdictRecord, f: Facts, *, latest: bool,
                  until: str | None) -> tuple[str, str, str] | None:
    """`latest`: the row is its reviewer's newest on the issue, so the issue's
    latest-attempt sections (follow-up, proposal, second opinion) speak to it.
    `until`: when that reviewer next ran on the issue, which ends this row's turn."""
    i = f.issues.get(row.subject)
    if i is None:
        return None
    if row.reviewer == "fix-review" and row.verdict == "safe":
        followup = i["fix_followup"] or {}
        caused = "caused" in ((followup.get("ci_attribution") or {}).get("files") or {}).values()
        if latest and (followup.get("closed_as") == "closed" or caused):
            return ("closed-unmerged", "unsafe",
                    "Our proposed PR's CI failed on the change" if caused
                    else "A maintainer closed our proposed PR unmerged")
        gaps = [o for o in i["second_opinion"] or [] if o.get("verdict") in GAPS]
        choice = i["fix_choice"] or {}
        if latest and gaps:
            return ("rival-better", "unsafe", f"#{gaps[0].get('pr')}'s tests found cases our fix "
                    "misses" + _quoted(gaps[0].get("why")))
        if latest and choice and _after(choice.get("at"), row):
            return ("rival-better", "unsafe",
                    f"Went with #{choice.get('pr')}" + _quoted(choice.get("reason")))
        for e in i["fix_thread"] or []:
            if (e.get("action") == "send-back" and e.get("source") == "operator"
                    and _after(e.get("at"), row)
                    and (until is None or str(e.get("at")) < until)):
                return ("sent-back", "unsafe",
                        f"{e.get('by')} sent the fix back" + _quoted(e.get("text")))
    if (latest and row.reviewer == "trust-boundary" and row.verdict == "hold"
            and (i["fix_run_proposal"] or {}).get("pr")):
        return ("proposed-over-hold", "pass", "The held fix was proposed anyway")
    if (row.reviewer == "fixability" and row.verdict == "skip"
            and row.data.get("kind") != "already-fixed"):
        scan = i["fix_scan"] or {}
        if scan.get("status") == "fixed" and _after(scan.get("checked_at"), row):
            return ("fixed-elsewhere", "go", f"The issue was fixed by #{scan.get('fixed_by')}")
    return None


def signals(facts: Facts) -> list[Signal]:
    out: list[Signal] = []
    newer: dict[tuple[str, int, str], str] = {}  # (kind, subject, reviewer) -> next run's `at`
    for row in sorted(facts.rows, key=lambda r: (r.at, r.id), reverse=True):
        key = (row.subject_kind, row.subject, row.reviewer)
        until = newer.get(key)
        newer[key] = row.at
        if row.label is not None:
            continue
        if row.subject_kind == "pr":
            found = _pr_signal(row, facts)
        elif row.subject_kind == "issue":
            found = _issue_signal(row, facts, latest=until is None, until=until)
        else:
            continue
        if found and found[1] != row.verdict:
            out.append(Signal(verdict_id=row.id, reviewer=row.reviewer,
                              subject_kind=row.subject_kind, subject=row.subject,
                              verdict=row.verdict, suggests=found[1], why=found[2],
                              key=found[0]))
    return out


def scorecard(window_rows: list[VerdictRecord],
              labeled_rows: list[VerdictRecord]) -> list[Card]:
    counts: dict[tuple[str, str, str], dict[str, int]] = {}

    def tally(r: VerdictRecord) -> dict[str, int]:
        return counts.setdefault((r.reviewer, r.prompt_sha, r.model), {})

    def add(c: dict[str, int], key: str) -> None:
        c[key] = c.get(key, 0) + 1

    for r in window_rows:
        c = tally(r)
        add(c, "runs")
        if r.verdict == verdict_log.VERDICTS[r.reviewer][-1]:
            add(c, "failed")
    for r in labeled_rows:
        if r.label is None:
            continue
        c = tally(r)
        add(c, "labeled")
        blocked = verdict_labels.blocks(r.reviewer, r.verdict)
        if verdict_labels.blocks(r.reviewer, r.label["expected"]):
            add(c, "must_catch")
            if blocked:
                add(c, "caught")
        else:
            add(c, "should_pass")
            if blocked:
                add(c, "blocked")
        call = verdict_labels.call(r.label, r.verdict, r.reviewer)
        if call != "right":
            add(c, call)
    cards = [Card(reviewer=k[0], prompt_sha=k[1], model=k[2], **v) for k, v in counts.items()]
    return sorted(cards, key=lambda c: (c.reviewer, -c.labeled, c.prompt_sha))


def _labeled_rows() -> list[VerdictRecord]:
    return data.store().review_verdicts(labeled=True)


def policy() -> dict:
    """The Policy page's reviewer section: each configuration's scorecard and
    the runs whose outcome suggests a label."""
    facts = gather(window_start())
    return {"scorecard": [asdict(c) for c in scorecard(facts.rows, _labeled_rows())],
            "queue": [asdict(s) for s in signals(facts)],
            "min_cases": MIN_CASES, "window_days": SIGNAL_WINDOW_DAYS}
