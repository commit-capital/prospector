"""Per-tab suggested pipeline actions — jobs worth running from the view whose
data they feed.

Each suggestion names a Control-tab job (`jobs.JOB_SPECS` kind) that is worth
running now — an ingest gone stale, a backlog a phase has never covered — with
a one-line reason, and a default batch size for jobs that take a count. The
builders are pure over plain data; `suggestions` gathers each view's inputs
from the app's cached snapshots. An empty list means nothing needs running
from that view.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import TypedDict

from pipeline import storekit


class Suggestion(TypedDict):
    kind: str
    title: str
    reason: str
    last_run: str | None
    count: int | None
    estimate_seconds: float | None


# An ingest older than this reads as stale and earns a refresh suggestion.
STALE_AFTER_HOURS = 24.0

# Default batch sizes, matching the Control tab's inputs and the CLIs' defaults.
CLUSTER_BATCH = 20
ISSUE_BATCH = 200
SWEEP_LIMIT = 12

VIEWS = ("prs", "issues", "alerts")


def _hours_since(iso: str | None, now_ts: float) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None
    return max(0.0, (now_ts - then) / 3600)


def _ago(iso: str | None, now_ts: float) -> str:
    hours = _hours_since(iso, now_ts)
    if hours is None:
        return "never run"
    if hours < 1:
        return "last run just now"
    if hours < 48:
        return f"last run {int(hours)}h ago"
    return f"last run {int(hours // 24)}d ago"


def _stale(iso: str | None, now_ts: float) -> bool:
    hours = _hours_since(iso, now_ts)
    return hours is None or hours >= STALE_AFTER_HOURS


def pr_suggestions(status: dict, now_ts: float) -> list[Suggestion]:
    """Suggestions for the PR-centric views, from `pipeline_status.status()`."""
    last_runs: dict[str, str | None] = {
        p["phase"]: p.get("last_run") for p in status.get("phases", [])}
    cov = status.get("coverage") or {}
    est = status.get("estimates") or {}
    out: list[Suggestion] = []

    ingest_last = last_runs.get("ingest")
    if _stale(ingest_last, now_ts):
        out.append({
            "kind": "ingest", "title": "Run ingest",
            "reason": f"PR metadata + signals are stale ({_ago(ingest_last, now_ts)})",
            "last_run": ingest_last, "count": None,
            "estimate_seconds": est.get("ingest_seconds"),
        })

    threat = cov.get("threat") or {}
    uncovered = threat.get("stale", 0) + threat.get("never", 0)
    if uncovered > 0:
        per_pr = est.get("threat_scan_seconds_per_pr")
        total = cov.get("total", 0)
        out.append({
            "kind": "threat-scan", "title": "Run threat scan",
            "reason": f"{uncovered} PR{'s' if uncovered != 1 else ''} lack a scan "
                      "of their latest push",
            "last_run": last_runs.get("threat-scan"), "count": None,
            "estimate_seconds": per_pr * total if per_pr is not None else None,
        })

    never_analyzed = (cov.get("analysis") or {}).get("never", 0)
    if never_analyzed > 0:
        count = min(CLUSTER_BATCH, never_analyzed)
        per_cluster = est.get("analyze_clusters_seconds_per_cluster")
        out.append({
            "kind": "analyze-clusters", "title": "Analyze clusters",
            "reason": f"{never_analyzed} PR{'s' if never_analyzed != 1 else ''} "
                      "have never been analyzed",
            "last_run": last_runs.get("analyze:commit"), "count": count,
            "estimate_seconds": per_cluster * count if per_cluster is not None else None,
        })
    return out


def issue_suggestions(status: dict, pending_fix_scan: int,
                      now_ts: float) -> list[Suggestion]:
    """Suggestions for the Issues view, from `pipeline_status.status()` plus the
    count of open issues whose fix scan is missing or stale."""
    last_runs: dict[str, str | None] = {
        p["phase"]: p.get("last_run") for p in status.get("phases", [])}
    icov = status.get("issue_coverage") or {}
    est = status.get("estimates") or {}
    out: list[Suggestion] = []

    ingest_last = last_runs.get("issue-ingest")
    if _stale(ingest_last, now_ts):
        out.append({
            "kind": "issue-ingest", "title": "Run issue ingest",
            "reason": f"issue metadata is stale ({_ago(ingest_last, now_ts)})",
            "last_run": ingest_last, "count": None, "estimate_seconds": None,
        })

    pending = icov.get("pending_analysis", 0)
    if pending > 0:
        count = min(ISSUE_BATCH, pending)
        per_issue = est.get("issue_analyze_seconds_per_issue")
        out.append({
            "kind": "issue-analyze", "title": "Analyze issues",
            "reason": f"{pending} open issue{'s' if pending != 1 else ''} "
                      "have no disposition yet",
            "last_run": last_runs.get("issue-analyze"), "count": count,
            "estimate_seconds": per_issue * count if per_issue is not None else None,
        })

    if pending_fix_scan > 0:
        out.append({
            "kind": "issue-find-fixed", "title": "Find fixed issues",
            "reason": f"{pending_fix_scan} open issue{'s' if pending_fix_scan != 1 else ''} "
                      "have no current fix scan",
            "last_run": None, "count": min(SWEEP_LIMIT, pending_fix_scan),
            "estimate_seconds": None,
        })
    return out


def alert_suggestions(last_ingest: str | None, unscanned_alerts: int,
                      unscanned_advisories: int, now_ts: float) -> list[Suggestion]:
    """The Alerts view's one suggestion: the security sweep, which ingests and
    fix-scans both alerts and advisories in one run."""
    parts: list[str] = []
    if _stale(last_ingest, now_ts):
        parts.append(f"alert ingest is stale ({_ago(last_ingest, now_ts)})")
    if unscanned_alerts > 0:
        parts.append(f"{unscanned_alerts} open alert{'s' if unscanned_alerts != 1 else ''} "
                     "with no current fix scan")
    if unscanned_advisories > 0:
        parts.append(f"{unscanned_advisories} open "
                     f"advisor{'ies' if unscanned_advisories != 1 else 'y'} "
                     "with no current fix scan")
    if not parts:
        return []
    return [{
        "kind": "security-sweep", "title": "Run security sweep",
        "reason": " · ".join(parts),
        "last_run": last_ingest, "count": SWEEP_LIMIT, "estimate_seconds": None,
    }]


def _latest_run(records: list[storekit.RunRecord], phase: str) -> str | None:
    latest: str | None = None
    for rec in records:
        if not isinstance(rec, storekit.PhaseRun) or rec.phase != phase:
            continue
        finished = rec.finished or rec.started
        if finished and (latest is None or finished > latest):
            latest = finished
    return latest


def suggestions(view: str) -> list[Suggestion]:
    """The named view's suggestions, gathered from live snapshots."""
    now_ts = time.time()
    if view == "prs":
        from prospector_app.backend import pipeline_status
        return pr_suggestions(pipeline_status.status(), now_ts)
    if view == "issues":
        from issue_triage.issue_freshness import is_current
        from prospector_app.backend import issues
        from prospector_app.backend import pipeline_status
        pending_fix_scan = sum(
            1 for i in issues.cached_issues().values()
            if i.state == "open" and not is_current(i, "fix_scan"))
        return issue_suggestions(pipeline_status.status(), pending_fix_scan, now_ts)
    if view == "alerts":
        from alert_triage.advisory_store import OPEN_STATES
        from alert_triage.alert_freshness import FIX_SCAN_MAX_AGE_DAYS, is_current
        from prospector_app.backend import advisory_data
        from prospector_app.backend import alert_data
        unscanned_alerts = sum(
            1 for a in alert_data.alerts().values()
            if a.state == "open"
            and not is_current(a, "fix_scan", max_age_days=FIX_SCAN_MAX_AGE_DAYS))
        unscanned_advisories = sum(
            1 for a in advisory_data.advisories().values()
            if a.state in OPEN_STATES
            and not is_current(a, "fix_scan", max_age_days=FIX_SCAN_MAX_AGE_DAYS))
        last_ingest = _latest_run(alert_data.runs(), "alert-ingest")
        return alert_suggestions(last_ingest, unscanned_alerts,
                                 unscanned_advisories, now_ts)
    raise ValueError(f"unknown view: {view}")
