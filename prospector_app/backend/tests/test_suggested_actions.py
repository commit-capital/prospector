"""The per-tab suggested-action builders: what earns a suggestion, what the
reason says, and the batch counts jobs are suggested with."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from prospector_app.backend import suggested_actions as sa

NOW = time.time()


def stamp(hours_ago: float) -> str:
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat()


def make_status(*, ingest_ago_h: float | None = 1.0, threat_uncovered: int = 0,
                analysis_never: int = 0, analysis_stale: int = 0, analysis_blocked: int = 0,
                issue_ingest_ago_h: float | None = 1.0,
                pending_analysis: int = 0, not_clustered: int = 0,
                cluster_blocked: int = 0, cluster_ago_h: float | None = None,
                estimates: dict | None = None) -> dict:
    phases = [
        {"phase": "ingest",
         "last_run": stamp(ingest_ago_h) if ingest_ago_h is not None else None},
        {"phase": "threat-scan", "last_run": stamp(2)},
        {"phase": "analyze:commit", "last_run": stamp(2)},
        {"phase": "issue-ingest",
         "last_run": stamp(issue_ingest_ago_h) if issue_ingest_ago_h is not None else None},
        {"phase": "issue-analyze", "last_run": stamp(2)},
        {"phase": "cluster",
         "last_run": stamp(cluster_ago_h) if cluster_ago_h is not None else None},
    ]
    return {
        "phases": phases,
        "coverage": {
            "total": 100,
            "threat": {"stale": threat_uncovered, "never": 0},
            "analysis": {"stale": analysis_stale, "never": analysis_never,
                         "blocked": analysis_blocked},
            "cluster": {"never": not_clustered, "blocked": cluster_blocked},
        },
        "issue_coverage": {"pending_analysis": pending_analysis},
        "estimates": estimates or {},
    }


def test_fresh_prs_suggest_nothing():
    assert sa.pr_suggestions(make_status(), NOW) == []


def test_stale_ingest_suggests_a_refresh():
    out = sa.pr_suggestions(make_status(ingest_ago_h=100), NOW)
    assert [s["kind"] for s in out] == ["ingest"]
    assert "last run 4d ago" in out[0]["reason"]


def test_ingest_from_yesterday_is_not_stale():
    assert sa.pr_suggestions(make_status(ingest_ago_h=30), NOW) == []


def test_never_run_ingest_reads_never():
    out = sa.pr_suggestions(make_status(ingest_ago_h=None), NOW)
    assert [s["kind"] for s in out] == ["ingest"]
    assert "never run" in out[0]["reason"]


def test_backlogs_suggest_scan_and_analysis_with_counts():
    status = make_status(
        threat_uncovered=7, analysis_never=45,
        estimates={"threat_scan_seconds": 75.0,
                   "analyze_clusters_seconds_per_cluster": 60.0})
    out = sa.pr_suggestions(status, NOW)
    kinds = {s["kind"]: s for s in out}
    assert set(kinds) == {"threat-scan", "analyze-clusters"}
    assert "7 PRs lack a scan" in kinds["threat-scan"]["reason"]
    assert kinds["threat-scan"]["estimate_seconds"] == 75.0
    assert kinds["analyze-clusters"]["count"] == sa.CLUSTER_BATCH
    assert kinds["analyze-clusters"]["estimate_seconds"] == 60.0 * sa.CLUSTER_BATCH


def test_small_analysis_backlog_caps_the_count_at_the_backlog():
    out = sa.pr_suggestions(make_status(analysis_never=3), NOW)
    assert [s["count"] for s in out] == [3]


def test_fresh_issues_suggest_nothing():
    assert sa.issue_suggestions(make_status(), 0, NOW) == []


def test_issue_backlogs_suggest_ingest_analysis_and_fix_scan():
    status = make_status(issue_ingest_ago_h=None, pending_analysis=300,
                         estimates={"issue_analyze_seconds_per_issue": 2.0})
    out = sa.issue_suggestions(status, 5, NOW)
    kinds = {s["kind"]: s for s in out}
    assert set(kinds) == {"issue-ingest", "issue-analyze", "issue-find-fixed"}
    assert kinds["issue-analyze"]["count"] == sa.ISSUE_BATCH
    assert kinds["issue-analyze"]["estimate_seconds"] == 2.0 * sa.ISSUE_BATCH
    assert "5 open issues have no current fix scan" in kinds["issue-find-fixed"]["reason"]
    assert kinds["issue-find-fixed"]["count"] == 5


def test_fresh_alerts_suggest_nothing():
    assert sa.alert_suggestions(stamp(1), 0, 0, NOW) == []


def test_never_ingested_alerts_suggest_the_sweep():
    out = sa.alert_suggestions(None, 0, 0, NOW)
    assert [s["kind"] for s in out] == ["security-sweep"]
    assert "never run" in out[0]["reason"]


def test_alert_and_advisory_backlogs_compose_one_sweep_reason():
    out = sa.alert_suggestions(stamp(1), 20, 1, NOW)
    assert len(out) == 1
    reason = out[0]["reason"]
    assert "20 open alerts with no current fix scan" in reason
    assert "1 open advisory with no current fix scan" in reason
    assert out[0]["count"] == sa.SWEEP_LIMIT


def test_unknown_view_is_refused():
    with pytest.raises(ValueError):
        sa.suggestions("bogus")


def test_unparseable_stamp_reads_as_never():
    out = sa.alert_suggestions("not-a-date", 0, 0, NOW)
    assert len(out) == 1
    assert "never run" in out[0]["reason"]


def test_unclustered_prs_suggest_clustering_when_it_has_not_run_today():
    out = sa.pr_suggestions(make_status(not_clustered=826, cluster_ago_h=29 * 24 + 1), NOW)
    assert [s["kind"] for s in out] == ["cluster-new"]
    assert out[0]["reason"] == "826 PRs not in a cluster (last clustering 29d ago)"
    assert out[0]["count"] == sa.CLUSTER_NEW_BATCH


def test_one_unclustered_pr_never_clustered():
    out = sa.pr_suggestions(make_status(not_clustered=1), NOW)
    assert out[0]["reason"] == "1 PR not in a cluster (never clustered)"
    assert out[0]["count"] == 1


def test_recent_clustering_quiets_the_suggestion():
    assert sa.pr_suggestions(make_status(not_clustered=5, cluster_ago_h=2), NOW) == []


def test_merged_views_drop_a_repeated_kind():
    a = {"kind": "ingest", "title": "a", "reason": "", "last_run": None, "count": None,
         "estimate_seconds": None}
    b = {**a, "kind": "issue-ingest"}
    assert sa.merged([[a], [{**a, "title": "dup"}, b]]) == [a, b]


def test_unclustered_prs_all_waiting_on_the_threat_scan_suggest_nothing():
    assert sa.pr_suggestions(make_status(not_clustered=3, cluster_blocked=3), NOW) == []


def test_a_backlog_partly_waiting_on_an_earlier_phase_says_which_to_run_first():
    out = sa.pr_suggestions(make_status(not_clustered=424, cluster_blocked=30,
                                        analysis_stale=125, analysis_never=692,
                                        analysis_blocked=424), NOW)
    kinds = {s["kind"]: s for s in out}
    assert kinds["cluster-new"]["reason"] == (
        "424 PRs not in a cluster (never clustered) · run Threat scan first to reach 30 of them")
    assert kinds["cluster-new"]["count"] == sa.CLUSTER_NEW_BATCH
    assert kinds["analyze-clusters"]["reason"] == (
        "817 PRs lack a current analysis · run Clustering first to reach 424 of them")


def test_a_job_run_within_the_cooldown_is_not_suggested_again():
    out = sa.issue_suggestions(make_status(), 444, NOW)
    assert [s["kind"] for s in out] == ["issue-find-fixed"]
    assert sa.rested(out, {"issue-find-fixed": stamp(5 / 60)}, NOW) == []
    assert sa.rested(out, {"issue-find-fixed": stamp(3 * 24)}, NOW) == []


def test_a_job_unrun_or_past_the_cooldown_stays_suggested():
    out = sa.issue_suggestions(make_status(), 444, NOW)
    assert sa.rested(out, {}, NOW) == out
    assert sa.rested(out, {"issue-find-fixed": None}, NOW) == out
    assert sa.rested(out, {"issue-find-fixed": stamp(sa.RECOMMEND_AFTER_HOURS + 1)}, NOW) == out


def test_suggestions_keep_a_job_whose_last_run_failed(monkeypatch):
    from prospector_app.backend import pipeline_status
    monkeypatch.setattr(pipeline_status, "status", lambda: make_status(threat_uncovered=3))
    monkeypatch.setattr(pipeline_status, "job_runtimes", lambda: {
        "threat-scan": {"last_run": stamp(0.1), "typical_seconds": None,
                        "typical_count": None, "failed_job": 4}})
    assert [s["kind"] for s in sa.suggestions("prs")] == ["threat-scan"]


def test_suggestions_drop_jobs_the_control_tab_shows_as_just_run(monkeypatch):
    from prospector_app.backend import pipeline_status
    monkeypatch.setattr(pipeline_status, "status", lambda: make_status(threat_uncovered=3))
    monkeypatch.setattr(pipeline_status, "job_runtimes", lambda: {
        "threat-scan": {"last_run": stamp(0.1), "typical_seconds": None, "typical_count": None}})
    assert sa.suggestions("prs") == []
