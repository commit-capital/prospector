"""Pipeline status — last-run times per phase and PR/issue coverage stats.

Reads the runs ledgers (PR store + issue store) for phase timing, the PR store
for freshness-split PR coverage (current / stale / never per fact, plus how
many open PRs' diffs a threat scan run here fetches), and the issue store for
the issue-analysis backlog.
"""
from __future__ import annotations

import statistics
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import diff_cache
from pipeline import freshness
from pipeline import storekit
from pipeline import threat_scan
from prospector_app.backend import data

if TYPE_CHECKING:
    from pipeline.model import Pr

# Canonical phase names → human label
PHASE_LABELS: dict[str, str] = {
    "ingest": "Ingest",
    "cluster": "Clustering",
    "analyze:commit": "Analysis",
    "threat-scan": "Threat scan",
    "security:commit": "Security review",
}

# The runs-ledger phases a card's freshness reads. A card names every ledger
# phase that keeps its fact current; a card absent here reads its own key.
PHASE_LEDGER: dict[str, tuple[str, ...]] = {
    "cluster": ("cluster:commit", "cluster:assign"),
    "threat-scan": ("threat-scan", "threat-scan:heads"),
}

# How many of the most recent samples an estimate averages over — recent
# enough to track a changed model/prompt/machine, many enough to smooth out
# one slow or lucky run.
_ESTIMATE_SAMPLES = 8

# Ledger phases whose records mix sweeps over every open PR with targeted
# reruns over a few (`threat_scan.py --only`, run by a reingest or a cluster
# triage), each with the stat counting the PRs a record covered. A record is a
# sweep when that count reaches _SWEEP_SHARE of the PRs open now; a targeted
# rerun's time is mostly startup and says nothing about a sweep's.
_SWEEP_STAT: dict[str, str] = {"threat-scan": "scanned"}
_SWEEP_SHARE = 0.25


def _last_runs(records: list[storekit.RunRecord]) -> dict[str, str]:
    """{phase: finished_at} for the most recent run of each phase in `records`."""
    latest: dict[str, str] = {}
    for rec in records:
        if not isinstance(rec, storekit.PhaseRun):
            continue
        finished = rec.finished or rec.started
        if finished and (rec.phase not in latest or finished > latest[rec.phase]):
            latest[rec.phase] = finished
    return latest


def _issue_runs() -> list[storekit.RunRecord]:
    """The issue pipeline's own runs ledger — a separate store from the PR
    store's, oldest first, served from the app's cached snapshot."""
    from prospector_app.backend import issues
    return issues.cached_runs()


def _elapsed_seconds(started: str | None, finished: str | None) -> float | None:
    start, end = storekit.parse_ts(started), storekit.parse_ts(finished)
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
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


def _is_sweep(rec: storekit.PhaseRun, open_prs: int) -> bool:
    stat = _SWEEP_STAT.get(rec.phase)
    if stat is None:
        return True
    n = rec.raw.get("stats", {}).get(stat)
    return isinstance(n, int) and n > 0 and n >= open_prs * _SWEEP_SHARE


def _count_stat(rec: storekit.PhaseRun, key: str) -> int:
    n = rec.raw.get("stats", {}).get(key)
    return n if isinstance(n, int) and n > 0 else 0


def _threat_scan_seconds(records: list[storekit.RunRecord], open_prs: int,
                         to_fetch: int) -> float | None:
    """Projected wall-clock of a threat-scan sweep over `open_prs` PRs that
    fetches `to_fetch` diffs, from the recent timed sweeps in `records`: a
    per-PR rate (the store load, the scan, the restamps), the median over
    sweeps that fetched nothing, plus a per-diff rate, the median over sweeps
    that fetched or failed to fetch diffs of their time beyond the per-PR part,
    per diff. With no fetch-free sweep, a fetching sweep's whole time is its
    fetches'. Medians, so one sweep on a slow store does not set a rate. None
    while the history cannot price the workload."""
    plain: list[float] = []
    fetching: list[tuple[float, int, int]] = []
    for rec in reversed(records):
        if (not isinstance(rec, storekit.PhaseRun) or rec.phase != "threat-scan"
                or not _is_sweep(rec, open_prs)):
            continue
        seconds = _elapsed_seconds(rec.started, rec.finished)
        if seconds is None:
            continue
        scanned = _count_stat(rec, "scanned")
        fetched = _count_stat(rec, "fetched") + _count_stat(rec, "fetch_failed")
        if fetched:
            if len(fetching) < _ESTIMATE_SAMPLES:
                fetching.append((seconds, scanned, fetched))
        elif len(plain) < _ESTIMATE_SAMPLES:
            plain.append(seconds / scanned)
    per_pr = statistics.median(plain) if plain else None
    if not to_fetch:
        return per_pr * open_prs if per_pr is not None else None
    if not fetching:
        return None
    base = per_pr or 0.0
    per_diff = statistics.median(max(seconds - base * scanned, 0.0) / fetched
                                 for seconds, scanned, fetched in fetching)
    return base * open_prs + per_diff * to_fetch


def _open_prs() -> int:
    return sum(1 for pr in data.prs().values() if pr.state == "open")


def _ledger_records(name: str) -> list[storekit.RunRecord]:
    """One named runs ledger, oldest first: the PR store's, the issue store's,
    or the alert store's."""
    if name == "pr":
        return data.runs()
    if name == "issue":
        return _issue_runs()
    from prospector_app.backend import alert_data
    return alert_data.runs()


def _job_history(kind: str) -> tuple[float, float | None, str] | None:
    """The mean duration of this machine's most recent successful runs of job
    `kind`, the mean count they were given (None for a job without one), and
    when the newest of them finished; None when it has none."""
    from prospector_app.backend import jobs
    samples: list[tuple[float, int | None, str]] = []
    for job in sorted(jobs.JOBS.values(), key=lambda j: j["id"], reverse=True):
        if job["kind"] != kind or job["status"] != "done" or job["finished"] is None:
            continue
        seconds = _elapsed_seconds(job["started"], job["finished"])
        if seconds is not None:
            samples.append((seconds, job["count"], job["finished"]))
        if len(samples) == _ESTIMATE_SAMPLES:
            break
    if not samples:
        return None
    counts = [c for _, c, _ in samples if c is not None]
    return (sum(s for s, _, _ in samples) / len(samples),
            sum(counts) / len(counts) if counts else None,
            samples[0][2])


def job_runtimes() -> dict[str, dict[str, str | float | None]]:
    """Per Control-tab job kind: when it last ran and how long a whole run
    typically takes. The duration is the mean of this machine's recent
    successful runs of the job, with `typical_count` the mean count they were
    given; without any, a job with a ledger mapping reads it from the ledger —
    each phase's mean over its recent timed records, summed over the job's
    phases — and it is None while any phase has no timed record. A phase in
    _SWEEP_STAT reads only its sweeps, for both the stamp and the duration. A
    job with neither is left out."""
    from prospector_app.backend import jobs
    ledgers: dict[str, list[storekit.RunRecord]] = {}
    out: dict[str, dict[str, str | float | None]] = {}
    for kind, spec in jobs.JOB_SPECS.items():
        history = _job_history(kind)
        ledger = spec.get("ledger")
        if ledger is None:
            if history is not None:
                out[kind] = {"last_run": history[2], "typical_seconds": history[0],
                             "typical_count": history[1]}
            continue
        name, phases = ledger
        if name not in ledgers:
            ledgers[name] = _ledger_records(name)
        records = ledgers[name]
        open_prs = _open_prs() if any(phase in _SWEEP_STAT for phase in phases) else 0
        last: str | None = None
        durations: dict[str, list[float]] = {phase: [] for phase in phases}
        for rec in reversed(records):
            if not isinstance(rec, storekit.PhaseRun) or rec.phase not in phases:
                continue
            if not _is_sweep(rec, open_prs):
                continue
            finished = rec.finished or rec.started
            if finished and (last is None or finished > last):
                last = finished
            seconds = _elapsed_seconds(rec.started, rec.finished)
            if seconds is not None and len(durations[rec.phase]) < _ESTIMATE_SAMPLES:
                durations[rec.phase].append(seconds)
        if history is not None:
            typical, count = history[0], history[1]
        else:
            typical = (sum(sum(d) / len(d) for d in durations.values())
                       if all(durations.values()) else None)
            count = None
        out[kind] = {"last_run": last, "typical_seconds": typical, "typical_count": count}
    return out


def _issue_coverage() -> dict:
    """Issue coverage counts: how many open issues have a current analysis vs.
    are still pending — the same missing-or-stale selection
    issue_analyze_driver.pending uses. Computed over the app's cached light
    issue snapshot, so a request does no whole-store fetch."""
    from issue_triage.issue_freshness import is_current
    from prospector_app.backend import issues
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
    """PR coverage split by freshness over the open PRs — the population every
    phase acts on; `tracked` is the whole store, closed and merged included.
    For each per-PR fact, `current` counts stamps computed against the PR's
    present head, `stale` stamps outdated by a later push, and `never` PRs no
    run has stamped. The threat scan reads diffs from this machine's local
    cache and first fetches the current-head diff of every open PR it holds
    none for, scanned or not, so `diff_uncached_here` counts the fetches the
    next scan run from this app makes (less the automation bumps it leaves
    unfetched) — a workload hint, not a coverage boundary."""
    tracked = len(all_prs)
    all_prs = {n: pr for n, pr in all_prs.items() if pr.state == "open"}
    total = len(all_prs)

    def split(section: str) -> dict[str, int]:
        current = sum(1 for pr in all_prs.values() if freshness.is_current(pr, section))
        present = sum(1 for pr in all_prs.values() if pr.section(section))
        return {"current": current, "stale": present - current, "never": total - present}

    threat = {"current": 0, "stale": 0, "never": 0, "diff_uncached_here": 0}
    for pr in all_prs.values():
        if pr.head_sha and not (diffs_dir / f"{pr.head_sha}.diff").exists():
            threat["diff_uncached_here"] += 1
        if threat_scan.stamped_at_head(pr):
            threat["current"] += 1
        else:
            threat["stale" if pr.section("threat") else "never"] += 1

    clustered = sum(1 for pr in all_prs.values() if pr.section("cluster"))
    return {
        "tracked": tracked,
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
    last_runs = _last_runs(pr_runs)

    phases = []
    for phase_key, label in PHASE_LABELS.items():
        ledger = PHASE_LEDGER.get(phase_key, (phase_key,))
        phases.append({
            "phase": phase_key,
            "label": label,
            "last_run": max((last_runs[k] for k in ledger if k in last_runs), default=None),
        })
    issue_runs = _issue_runs()
    last_issue_runs = _last_runs(issue_runs)
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

    coverage = _pr_coverage(data.prs(), diff_cache.DIFFS)
    return {
        "phases": phases,
        "coverage": coverage,
        "issue_coverage": _issue_coverage(),
        # Rough durations from recent history: a whole ingest, a whole threat
        # scan over this machine's open PRs and diff fetches, and per-unit
        # rates for the Control tab to project onto however many clusters or
        # issues a job is about to run over. None where the history holds no
        # timed run to sample from.
        "estimates": {
            "ingest_seconds": _seconds_per_run(pr_runs, "ingest"),
            "threat_scan_seconds": _threat_scan_seconds(
                pr_runs, coverage["total"], coverage["threat"]["diff_uncached_here"]),
            "analyze_clusters_seconds_per_cluster": _seconds_per_unit(pr_runs, "analyze:commit", "attempted"),
            "issue_analyze_seconds_per_issue": _seconds_per_unit(issue_runs, "analyze", "attempted"),
        },
    }
