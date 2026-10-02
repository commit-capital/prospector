"""pipeline_status: last-run reduction logic and PR coverage splits."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import storekit
from pipeline.model import Pr
from prospector_app.backend import pipeline_status


def test_last_runs_picks_latest_per_phase():
    records = [storekit.parse_run(d) for d in [
        {"phase": "ingest", "finished": "2026-06-01T10:00:00+00:00", "started": "2026-06-01T09:00:00+00:00"},
        {"phase": "ingest", "finished": "2026-06-02T10:00:00+00:00", "started": "2026-06-02T09:00:00+00:00"},
        {"phase": "cluster:commit", "finished": "2026-06-01T11:00:00+00:00"},
        {"phase": "ingest", "finished": "2026-06-01T08:00:00+00:00"},
    ]]
    result = pipeline_status._last_runs(records)

    assert result["ingest"] == "2026-06-02T10:00:00+00:00"
    assert result["cluster:commit"] == "2026-06-01T11:00:00+00:00"
    assert "analyze:commit" not in result


def test_last_runs_falls_back_to_started_when_no_finished():
    records = [storekit.parse_run(d) for d in [
        {"phase": "threat-scan", "started": "2026-06-03T07:00:00+00:00"},
    ]]
    result = pipeline_status._last_runs(records)

    assert result["threat-scan"] == "2026-06-03T07:00:00+00:00"


def test_last_runs_empty_store_returns_empty():
    assert pipeline_status._last_runs([]) == {}


def test_last_issue_runs_reads_issue_ledger():
    records = [storekit.parse_run(d) for d in [
        {"phase": "ingest", "started": "2026-07-01T09:00:00+00:00",
         "finished": "2026-07-01T10:00:00+00:00", "stats": {}},
        {"phase": "ingest", "started": "2026-07-02T09:00:00+00:00",
         "finished": "2026-07-02T10:00:00+00:00", "stats": {}},
        {"phase": "cluster", "finished": "2026-07-03T10:00:00+00:00"},
    ]]

    runs = pipeline_status._last_runs(records)
    assert runs["ingest"] == "2026-07-02T10:00:00+00:00"
    assert runs["cluster"] == "2026-07-03T10:00:00+00:00"


def test_last_issue_runs_skips_untimestamped_records():
    """Runs recorded without started/finished stamps don't produce a last-run."""
    records = [storekit.parse_run({"phase": "ingest", "issues": 3})]

    assert pipeline_status._last_runs(records) == {}


def test_last_runs_skips_records_with_no_phase():
    records = [
        {"phase": "", "finished": "2026-06-01T10:00:00+00:00"},
        {"finished": "2026-06-01T10:00:00+00:00"},
    ]
    assert pipeline_status._last_runs(records) == {}


def _seed_issue_store(tmp_path, monkeypatch):
    from issue_triage import issue_store
    from prospector_app.backend import issues
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    st = issue_store.IssueStore(tmp_path)
    meta = {"title": "t", "body": "b", "state": "open", "updated_at": "T1"}
    st.create_issue(1, meta)
    st.create_issue(2, meta)
    st.create_issue(3, {**meta, "state": "closed"})
    st.edit_issue(1).route_to("needs-human", "r")
    return st


def test_issue_coverage_counts_open_pending(tmp_path, monkeypatch):
    """Coverage mirrors issue_analyze_driver.pending: open issues whose analysis
    is missing or stale are the backlog; closed issues don't count as open."""
    _seed_issue_store(tmp_path, monkeypatch)
    cov = pipeline_status._issue_coverage()
    assert cov == {"total": 3, "open": 2, "analyzed": 1, "pending_analysis": 1}


def _cov_pr(n: int, head: str, state: str = "open", **sections) -> Pr:
    rec: dict = {"pr": n,
                 "meta": {"title": "t", "author": "a", "state": state, "draft": False,
                          "head_sha": head, "checked_at": "2026-07-01T00:00:00+00:00"}}
    rec.update(sections)
    return Pr(None, rec)


def test_pr_coverage_splits_current_stale_never(tmp_path):
    """Coverage distinguishes facts computed against the PR's present head
    (current) from stamps an intervening push outdated (stale) and PRs no run
    has reached (never); the threat split also counts the open PRs, scanned or
    not, whose current-head diff a scan run here has to fetch."""
    stamp = {"checked_at": "2026-07-01T00:00:00+00:00"}
    prs = {
        1: _cov_pr(1, "h1",
                   threat={"verdict": "clear", "signatures": [], **stamp, "against_head_sha": "h1"},
                   analysis={"disposition": "merge", **stamp, "against_head_sha": "h1"},
                   security={"verdict": "GREEN", **stamp, "against_head_sha": "h1"},
                   cluster={"ids": [3], **stamp, "against_head_sha": "h1"}),
        2: _cov_pr(2, "h2",
                   threat={"verdict": "clear", "signatures": [], **stamp, "against_head_sha": "OLD"},
                   analysis={"disposition": "merge", **stamp, "against_head_sha": "OLD"}),
        3: _cov_pr(3, "h3"),
        4: _cov_pr(4, "h4"),
        # closed PRs are tracked but outside every phase's population
        5: _cov_pr(5, "h5", state="closed",
                   threat={"verdict": "clear", "signatures": [], **stamp, "against_head_sha": "h5"},
                   cluster={"ids": [3], **stamp, "against_head_sha": "h5"}),
        6: _cov_pr(6, "h6", state="merged"),
    }
    (tmp_path / "h2.diff").write_text("diff --git a/x b/x\n")
    (tmp_path / "h3.diff").write_text("diff --git a/x b/x\n")
    (tmp_path / "h5.diff").write_text("diff --git a/x b/x\n")

    cov = pipeline_status._pr_coverage(prs, tmp_path)

    assert cov["tracked"] == 6
    assert cov["total"] == 4
    assert cov["clustered"] == 1 and cov["not_clustered"] == 3
    assert cov["analysis"] == {"current": 1, "stale": 1, "never": 2}
    assert cov["security"] == {"current": 1, "stale": 0, "never": 3}
    # h1 is scanned at its head yet uncached here; the scan fetches it too.
    assert cov["threat"] == {"current": 1, "stale": 1, "never": 2,
                             "diff_uncached_here": 2}


def test_issue_coverage_serves_from_cached_snapshot(tmp_path, monkeypatch):
    """One request loads the issue snapshot; a repeat request inside the debounce
    window recomputes coverage from it with no further store row fetches."""
    from issue_triage.issue_store import IssueStore
    _seed_issue_store(tmp_path, monkeypatch)
    fetches = {"n": 0}
    orig_all, orig_since = IssueStore.all_issues, IssueStore.issues_since

    def counting_all(self, **kw):
        fetches["n"] += 1
        return orig_all(self, **kw)

    def counting_since(self, watermark, **kw):
        fetches["n"] += 1
        return orig_since(self, watermark, **kw)

    monkeypatch.setattr(IssueStore, "all_issues", counting_all)
    monkeypatch.setattr(IssueStore, "issues_since", counting_since)

    first = pipeline_status._issue_coverage()
    after_first = fetches["n"]
    second = pipeline_status._issue_coverage()

    assert first == second == {"total": 3, "open": 2, "analyzed": 1, "pending_analysis": 1}
    assert fetches["n"] == after_first


def test_issue_runs_served_from_cached_snapshot(tmp_path, monkeypatch):
    """Repeat requests read the issue runs ledger from the cached snapshot, not
    a fresh whole-ledger fetch per request."""
    from issue_triage.issue_store import IssueStore
    st = _seed_issue_store(tmp_path, monkeypatch)
    st.append_run({"phase": "ingest", "started": "2026-07-01T09:00:00+00:00",
                   "finished": "2026-07-01T10:00:00+00:00", "stats": {}})
    reads = {"n": 0}
    orig = IssueStore.runs_after

    def counting(self, rowid, held=()):
        reads["n"] += 1
        return orig(self, rowid, held)

    monkeypatch.setattr(IssueStore, "runs_after", counting)

    pipeline_status._issue_runs()
    after_first = reads["n"]
    second = pipeline_status._issue_runs()

    assert pipeline_status._last_runs(second)["ingest"] == "2026-07-01T10:00:00+00:00"
    assert reads["n"] == after_first


def test_apply_verdicts_stamps_analyze_last_run(tmp_path, monkeypatch):
    """apply_verdicts appends a finished-stamped 'analyze' run record, so the
    Control tab's Issue analysis tile shows when analysis last ran."""
    from issue_triage import issue_analyze_driver
    st = _seed_issue_store(tmp_path, monkeypatch)
    issue_analyze_driver.apply_verdicts(st, [
        {"issue": 2, "disposition": "needs-human", "rationale": "r"}])
    assert "analyze" in pipeline_status._last_runs(st.runs())


def test_seconds_per_unit_averages_recent_samples():
    records = [storekit.parse_run(d) for d in [
        {"phase": "threat-scan", "started": "2026-07-01T10:00:00+00:00",
         "finished": "2026-07-01T10:00:10+00:00", "stats": {"scanned": 10}},
        {"phase": "threat-scan", "started": "2026-07-02T10:00:00+00:00",
         "finished": "2026-07-02T10:00:20+00:00", "stats": {"scanned": 10}},
    ]]

    rate = pipeline_status._seconds_per_unit(records, "threat-scan", "scanned")
    assert rate == 1.5  # (1.0 + 2.0) / 2 seconds/PR


def test_seconds_per_unit_skips_zero_duration_and_missing_count():
    records = [storekit.parse_run(d) for d in [
        # analyze_clusters.py's old stamping bug: started == finished.
        {"phase": "analyze:commit", "started": "2026-07-01T10:00:00+00:00",
         "finished": "2026-07-01T10:00:00+00:00", "stats": {"attempted": 5}},
        # no `attempted` in stats.
        {"phase": "analyze:commit", "started": "2026-07-02T10:00:00+00:00",
         "finished": "2026-07-02T10:00:30+00:00", "stats": {"committed": 5}},
    ]]

    assert pipeline_status._seconds_per_unit(records, "analyze:commit", "attempted") is None


def test_seconds_per_unit_none_when_phase_never_ran():
    assert pipeline_status._seconds_per_unit([], "threat-scan", "scanned") is None


def test_seconds_per_run_averages_whole_run_durations():
    records = [storekit.parse_run(d) for d in [
        {"phase": "ingest", "started": "2026-07-01T10:00:00+00:00",
         "finished": "2026-07-01T10:00:04+00:00"},
        {"phase": "ingest", "started": "2026-07-02T10:00:00+00:00",
         "finished": "2026-07-02T10:00:06+00:00"},
    ]]

    assert pipeline_status._seconds_per_run(records, "ingest") == 5.0


def _scan(seconds: int, *, scanned: int, fetched: int | None = None, failed: int = 0,
          day: int = 1) -> dict:
    start = datetime(2026, 7, day, 10, tzinfo=timezone.utc)
    stats: dict[str, int] = {"scanned": scanned}
    if fetched is not None:
        stats.update(fetched=fetched, fetch_failed=failed)
    return {"phase": "threat-scan", "started": start.isoformat(),
            "finished": (start + timedelta(seconds=seconds)).isoformat(), "stats": stats}


def _scan_seconds(runs: list[dict], open_prs: int, to_fetch: int) -> float | None:
    return pipeline_status._threat_scan_seconds(
        [storekit.parse_run(d) for d in runs], open_prs, to_fetch)


def test_one_pr_rescans_do_not_price_a_threat_scan_sweep():
    """A reingest's one-PR rescan is mostly startup: eight of them beside one
    sweep leave the projection at the sweep's own time."""
    runs = [_scan(60, scanned=3000, fetched=0),
            *[_scan(20, scanned=1, fetched=0) for _ in range(7)],
            _scan(274, scanned=1, fetched=0)]

    assert _scan_seconds(runs, 3000, 0) == pytest.approx(60.0)


def test_a_threat_scan_sweep_is_priced_per_pr_plus_per_fetched_diff():
    runs = [_scan(50, scanned=1000, fetched=0),
            _scan(850, scanned=1000, fetched=390, failed=10)]

    # 0.05s a PR; the fetching sweep's other 800s over its 400 attempts is 2s a diff.
    assert _scan_seconds(runs, 1200, 300) == pytest.approx(0.05 * 1200 + 2.0 * 300)
    assert _scan_seconds(runs, 1200, 0) == pytest.approx(0.05 * 1200)


def test_a_sweep_without_fetch_counters_fetched_nothing():
    assert _scan_seconds([_scan(40, scanned=1000)], 1000, 0) == pytest.approx(40.0)


def test_one_slow_sweep_does_not_set_the_threat_scan_rate():
    runs = [_scan(60, scanned=1000), _scan(4000, scanned=1000), _scan(70, scanned=1000)]

    assert _scan_seconds(runs, 1000, 0) == pytest.approx(70.0)


def test_with_no_fetch_free_sweep_a_fetching_sweeps_time_is_its_fetches():
    runs = [_scan(500, scanned=1000, fetched=250)]

    assert _scan_seconds(runs, 1000, 100) == pytest.approx(200.0)


def test_a_threat_scan_estimate_is_none_until_history_prices_the_workload():
    plain = [_scan(60, scanned=1000, fetched=0)]
    fetching = [_scan(500, scanned=1000, fetched=250)]

    assert _scan_seconds([], 1000, 0) is None
    assert _scan_seconds([_scan(20, scanned=1)], 1000, 0) is None
    assert _scan_seconds(plain, 1000, 5) is None
    assert _scan_seconds(fetching, 1000, 0) is None


def test_the_threat_scan_job_reads_its_sweeps_alone(monkeypatch, tmp_path):
    from prospector_app.backend import data, issues
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(data, "prs", lambda: {n: _cov_pr(n, f"h{n}") for n in range(1, 9)})
    monkeypatch.setattr(data, "runs", lambda: [storekit.parse_run(d) for d in [
        _scan(60, scanned=8, fetched=0, day=1),
        _scan(20, scanned=1, fetched=0, day=2),
    ]])

    assert pipeline_status.job_runtimes()["threat-scan"] == {
        "last_run": "2026-07-01T10:01:00+00:00", "typical_seconds": 60.0,
        "typical_count": None}


def test_job_runtimes_reads_each_jobs_ledger_phases(monkeypatch, tmp_path):
    """Each job kind with a ledger mapping reports its phases' latest run and
    the typical whole-run duration averaged over recent runs."""
    from prospector_app.backend import alert_data, data, issues
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(data, "prs", lambda: {})
    monkeypatch.setattr(data, "runs", lambda: [storekit.parse_run(d) for d in [
        {"phase": "ingest", "started": "2026-07-01T10:00:00+00:00",
         "finished": "2026-07-01T10:00:04+00:00"},
        {"phase": "ingest", "started": "2026-07-02T10:00:00+00:00",
         "finished": "2026-07-02T10:00:06+00:00"},
        {"phase": "verify:single", "started": "2026-07-03T10:00:00+00:00",
         "finished": "2026-07-03T10:02:00+00:00"},
    ]])
    monkeypatch.setattr(alert_data, "runs", lambda: [storekit.parse_run(d) for d in [
        {"phase": "alert-ingest", "started": "2026-07-04T10:00:00+00:00",
         "finished": "2026-07-04T10:00:30+00:00"},
        {"phase": "advisory-find-fixed", "started": "2026-07-05T10:00:00+00:00",
         "finished": "2026-07-05T10:01:00+00:00"},
    ]])

    runtimes = pipeline_status.job_runtimes()

    assert runtimes["ingest"] == {"last_run": "2026-07-02T10:00:06+00:00",
                                  "typical_seconds": 5.0, "typical_count": None}
    assert runtimes["verify-pr"] == {"last_run": "2026-07-03T10:02:00+00:00",
                                     "typical_seconds": 120.0, "typical_count": None}
    # A multi-phase ledger reads as the latest of any of its phases, and has no
    # typical duration while any of its phases has never recorded one.
    assert runtimes["security-sweep"]["last_run"] == "2026-07-05T10:01:00+00:00"
    assert runtimes["security-sweep"]["typical_seconds"] is None
    # Never run → no stamp and no duration.
    assert runtimes["threat-scan"] == {"last_run": None, "typical_seconds": None,
                                       "typical_count": None}
    # No ledger mapping → not reported at all.
    assert "selftest" not in runtimes
    assert "triage-cluster" not in runtimes


def _sweep_ledger(monkeypatch, tmp_path):
    from prospector_app.backend import alert_data, data, issues
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(data, "runs", lambda: [])
    monkeypatch.setattr(data, "prs", lambda: {})
    monkeypatch.setattr(alert_data, "runs", lambda: [storekit.parse_run(d) for d in [
        {"phase": phase, "started": "2026-07-04T10:00:00+00:00",
         "finished": f"2026-07-04T10:{minutes:02d}:00+00:00"}
        for phase, minutes in (("alert-ingest", 1), ("alert-find-fixed", 4),
                               ("advisory-ingest", 1), ("advisory-find-fixed", 2))
    ]])


def test_a_multi_phase_job_takes_the_sum_of_its_phases(monkeypatch, tmp_path):
    _sweep_ledger(monkeypatch, tmp_path)
    assert pipeline_status.job_runtimes()["security-sweep"]["typical_seconds"] == 480.0


def test_the_jobs_own_recent_runs_override_the_ledger(monkeypatch, tmp_path):
    from prospector_app.backend import jobs
    _sweep_ledger(monkeypatch, tmp_path)
    for minutes, count in ((6, 12), (10, 20)):
        job = jobs.start_job("security-sweep", count=count)
        job.update(status="done", started="2026-07-05T10:00:00",
                   finished=f"2026-07-05T10:{minutes:02d}:00")
    failed = jobs.start_job("security-sweep", count=12)
    failed.update(status="failed", started="2026-07-05T10:00:00",
                  finished="2026-07-05T11:00:00")
    sweep = pipeline_status.job_runtimes()["security-sweep"]
    assert (sweep["typical_seconds"], sweep["typical_count"]) == (480.0, 16.0)


def test_a_job_without_a_ledger_reads_its_own_runs(monkeypatch, tmp_path):
    from prospector_app.backend import jobs
    _sweep_ledger(monkeypatch, tmp_path)
    assert "triage-cluster" not in pipeline_status.job_runtimes()
    job = jobs.start_job("triage-cluster", cluster=7)
    job.update(status="done", started="2026-07-05T10:00:00+00:00",
               finished="2026-07-05T10:03:00+00:00")
    assert pipeline_status.job_runtimes()["triage-cluster"] == {
        "last_run": "2026-07-05T10:03:00+00:00", "typical_seconds": 180.0,
        "typical_count": None}


def test_elapsed_reads_a_stamp_without_an_offset_as_utc():
    assert pipeline_status._elapsed_seconds("2026-07-05T10:00:00",
                                            "2026-07-05T10:01:00+00:00") == 60.0


def test_clustering_freshness_reads_the_latest_of_commit_and_assign(monkeypatch, tmp_path):
    from prospector_app.backend import data, issues
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(data, "runs", lambda: [storekit.parse_run(d) for d in [
        {"phase": "cluster:commit", "started": "2026-06-19T15:00:00+00:00",
         "finished": "2026-06-19T15:51:26+00:00"},
        {"phase": "cluster:assign", "started": "2026-08-27T21:00:00+00:00",
         "finished": "2026-08-27T21:05:42+00:00"},
    ]])
    monkeypatch.setattr(data, "prs", lambda: {})

    phases = {p["phase"]: p for p in pipeline_status.status()["phases"]}

    assert phases["cluster"]["label"] == "Clustering"
    assert phases["cluster"]["last_run"] == "2026-08-27T21:05:42+00:00"


def test_status_includes_estimates(monkeypatch, tmp_path):
    from pipeline import diff_cache
    from prospector_app.backend import data, issues
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(data, "runs", lambda: [storekit.parse_run(d) for d in [
        _scan(10, scanned=10),
        _scan(30, scanned=10, fetched=5),
    ]])
    monkeypatch.setattr(data, "prs", lambda: {n: _cov_pr(n, f"h{n}") for n in range(1, 11)})
    monkeypatch.setattr(diff_cache, "DIFFS", tmp_path)
    for n in range(1, 8):
        (tmp_path / f"h{n}.diff").write_text("diff --git a/x b/x\n")

    result = pipeline_status.status()

    # 1s a PR over ten open PRs, plus 4s a diff for the three uncached here.
    assert result["estimates"]["threat_scan_seconds"] == 10.0 + 4.0 * 3
    assert result["estimates"]["ingest_seconds"] is None
    assert result["estimates"]["analyze_clusters_seconds_per_cluster"] is None
    assert result["estimates"]["issue_analyze_seconds_per_issue"] is None


def test_status_reads_the_pr_ledger_once(monkeypatch, tmp_path):
    from prospector_app.backend import data, issues
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    reads: list[int] = []
    monkeypatch.setattr(data, "runs", lambda: reads.append(1) or [])
    monkeypatch.setattr(data, "prs", lambda: {})

    pipeline_status.status()

    assert len(reads) == 1
