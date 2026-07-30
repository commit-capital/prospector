"""Pipeline status — last-run times per phase and PR/issue coverage stats.

Reads the runs ledgers (PR store + issue store) for phase timing, the PR store
for freshness-split PR coverage (current / stale / never per fact, plus how
many of the threat scan's uncovered PRs already have a locally cached diff vs.
need its on-demand fetch), and the issue store for the issue-analysis backlog.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import diff_cache
from pipeline import freshness
from pipeline import storekit
from app.backend import data

if TYPE_CHECKING:
    from pipeline.model import Pr

# Canonical phase names → human label
PHASE_LABELS: dict[str, str] = {
    "ingest": "Ingest",
    "cluster:commit": "Clustering",
    "analyze:commit": "Analysis",
    "threat-scan": "Threat scan",
    "security:commit": "Security review",
}

# How many of the most recent samples an estimate averages over — recent
# enough to track a changed model/prompt/machine, many enough to smooth out
# one slow or lucky run.
_ESTIMATE_SAMPLES = 8


def _last_runs() -> dict[str, str]:
    """Return {phase: finished_at} for the most recent run of each phase."""
    latest: dict[str, str] = {}
    for rec in data.runs():
        if not isinstance(rec, storekit.PhaseRun):
            continue
        finished = rec.finished or rec.started
        if finished and (rec.phase not in latest or finished > latest[rec.phase]):
            latest[rec.phase] = finished
    return latest


def _issue_runs() -> list[storekit.RunRecord]:
    """The issue pipeline's own runs ledger — a separate store from the PR
    store's, oldest first, served from the cockpit's cached snapshot."""
    from app.backend import issues
    return issues.cached_runs()


def _last_issue_runs(issue_runs: list[storekit.RunRecord]) -> dict[str, str]:
    """{phase: finished_at} for the most recent run of each issue-pipeline phase."""
    latest: dict[str, str] = {}
    for rec in issue_runs:
        if not isinstance(rec, storekit.PhaseRun):
            continue
        finished = rec.finished or rec.started
        if finished and (rec.phase not in latest or finished > latest[rec.phase]):
            latest[rec.phase] = finished
    return latest


def _elapsed_seconds(started: str | None, finished: str | None) -> float | None:
    if not started or not finished:
        return None
    try:
        delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
    except ValueError:
        return None
    seconds = delta.total_seconds()
    return seconds if seconds > 0 else None


def _seconds_per_unit(records: list[storekit.RunRecord], phase: str, count_key: str) -> float | None:
    """Average seconds-per-unit for `phase`, over the most recent runs that
    recorded a real elapsed duration alongside a positive `stats.<count_key>`
    (a run stamped `started == finished`, or missing the count, is skipped —
    it carries no rate information). None until at least one usable sample
    exists."""
    rates: list[float] = []
    for rec in reversed(records):
        if not isinstance(rec, storekit.PhaseRun) or rec.phase != phase:
            continue
        seconds = _elapsed_seconds(rec.started, rec.finished)
        if seconds is None:
            continue
        n = rec.raw.get("stats", {}).get(count_key)
        if not isinstance(n, int) or n <= 0:
            continue
        rates.append(seconds / n)
        if len(rates) >= _ESTIMATE_SAMPLES:
            break
    return sum(rates) / len(rates) if rates else None


def _seconds_per_run(records: list[storekit.RunRecord], phase: str) -> float | None:
    """Average whole-run duration for `phase`, over the most recent runs with a
    real elapsed duration — for phases with no per-unit count to divide by
    (e.g. ingest always processes the whole open-PR set)."""
    durations: list[float] = []
    for rec in reversed(records):
        if not isinstance(rec, storekit.PhaseRun) or rec.phase != phase:
            continue
        seconds = _elapsed_seconds(rec.started, rec.finished)
        if seconds is None:
            continue
        durations.append(seconds)
        if len(durations) >= _ESTIMATE_SAMPLES:
            break
    return sum(durations) / len(durations) if durations else None


def _issue_coverage() -> dict:
    """Issue coverage counts: how many open issues have a current analysis vs.
    are still pending — the same missing-or-stale selection
    issue_analyze_driver.pending uses. Computed over the cockpit's cached light
    issue snapshot, so a request does no whole-store fetch."""
    from issue_triage.issue_freshness import is_current
    from app.backend import issues
    all_issues = issues.cached_issues()
    open_issues = [i for i in all_issues.values() if i.state == "open"]
    pending = sum(1 for i in open_issues if not is_current(i, "analysis"))
    return {
        "total": len(all_issues),
        "open": len(open_issues),
        "analyzed": len(open_issues) - pending,
        "pending_analysis": pending,
    }


def _pr_coverage(all_prs: dict[int, Pr], diffs_dir: Path) -> dict:
    """PR coverage split by freshness. For each per-PR fact, `current` counts
    stamps computed against the PR's present head, `stale` stamps outdated by a
    later push, and `never` PRs no run has stamped. The threat scan reads diffs
    from this machine's local cache and fetches missing open-PR diffs on
    demand, so the split of its uncovered PRs (stale + never) into
    `diff_cached_here` (current head's diff already cached) and
    `diff_uncached_here` (the scan fetches it as it runs) is a workload hint —
    how much fetching the next scan run from this cockpit will do."""
    total = len(all_prs)

    def split(section: str) -> dict[str, int]:
        current = sum(1 for pr in all_prs.values() if freshness.is_current(pr, section))
        present = sum(1 for pr in all_prs.values() if pr.section(section))
        return {"current": current, "stale": present - current, "never": total - present}

    # `threat` is not freshness-governed (a malicious verdict gates sticky, at
    # any head), so currency here is a direct sha comparison: has the code at
    # the PR's present head been signature-scanned?
    threat = {"current": 0, "stale": 0, "never": 0,
              "diff_cached_here": 0, "diff_uncached_here": 0}
    for pr in all_prs.values():
        sec = pr.section("threat")
        if sec and sec.get("against_head_sha") == pr.head_sha:
            threat["current"] += 1
            continue
        threat["stale" if sec else "never"] += 1
        cached = (diffs_dir / f"{pr.head_sha}.diff").exists()
        threat["diff_cached_here" if cached else "diff_uncached_here"] += 1

    clustered = sum(1 for pr in all_prs.values() if pr.section("cluster"))
    return {
        "total": total,
        "clustered": clustered,
        "not_clustered": total - clustered,
        "analysis": split("analysis"),
        "security": split("security"),
        "threat": threat,
    }


def status() -> dict:
    """Return pipeline phase timing + PR and issue coverage stats."""
    pr_runs = data.runs()
    last_runs = _last_runs()

    phases = []
    for phase_key, label in PHASE_LABELS.items():
        phases.append({
            "phase": phase_key,
            "label": label,
            "last_run": last_runs.get(phase_key),
        })
    issue_runs = _issue_runs()
    last_issue_runs = _last_issue_runs(issue_runs)
    phases.append({
        "phase": "issue-ingest",
        "label": "Issue ingest",
        "last_run": last_issue_runs.get("ingest"),
    })
    phases.append({
        "phase": "issue-analyze",
        "label": "Issue analysis",
        "last_run": last_issue_runs.get("analyze"),
    })

    return {
        "phases": phases,
        "coverage": _pr_coverage(data.prs(), diff_cache.DIFFS),
        "issue_coverage": _issue_coverage(),
        # Rough per-unit durations from recent history, for the Control tab to
        # project "~this long" onto however many PRs/clusters/issues a Quick
        # Action button is about to run over. None where no run has yet
        # recorded a real (non-zero, count-tagged) duration to sample from.
        "estimates": {
            "ingest_seconds": _seconds_per_run(pr_runs, "ingest"),
            "threat_scan_seconds_per_pr": _seconds_per_unit(pr_runs, "threat-scan", "scanned"),
            "analyze_clusters_seconds_per_cluster": _seconds_per_unit(pr_runs, "analyze:commit", "attempted"),
            "issue_analyze_seconds_per_issue": _seconds_per_unit(issue_runs, "analyze", "attempted"),
        },
    }
