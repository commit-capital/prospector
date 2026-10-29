"""Draining a machine's worker lanes before a restart."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_triage.issue_store import IssueStore
from pipeline import store as S
from prospector_app.backend import data, host_drain, lane_pause
from prospector_app.backend.tests.test_autohunt import _clean_merge_pr


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    return st


@pytest.fixture
def issues(tmp_path, monkeypatch):
    s = IssueStore(tmp_path / "issues")
    for n in (1, 2, 3):
        s.save_issue({"issue": n, "meta": {"title": f"bug {n}", "state": "open",
                                           "updated_at": _ago(0), "created_at": _ago(0)}})
    monkeypatch.setattr(host_drain, "IssueStore", lambda: s)
    return s


def _request(issues: IssueStore, n: int, status: str, started: float,
             finished: float | None = None, host: str = "studio") -> None:
    issues.edit_issue(n).record_fix_request({
        "action": "solve", "source": "hunter", "status": status, "host": host,
        "started_at": _ago(started),
        **({"finished_at": _ago(finished)} if finished is not None else {})})


def _beat(st: S.Store, current: list[int], at: str | None = None) -> None:
    st.save_issue_fix_worker({"host": "studio", "pid": 1, "last_beat": at or _ago(0),
                              "current_issue": min(current) if current else None,
                              "current_issues": current, "autohunt": False})


def test_pause_leaves_an_operator_pause_and_resume_lifts_only_its_own(store):
    lane_pause.pause("studio", "verify", by="ops")
    assert "verify" not in host_drain.pause()
    assert lane_pause.paused("studio", "issue-fix")
    assert set(host_drain.resume()) == set(host_drain.worker_health.LANES) - {"verify"}
    assert lane_pause.paused("studio", "verify")
    assert not lane_pause.paused("studio", "issue-fix")


def test_a_run_reads_its_elapsed_time_against_this_hosts_finished_runs(store, issues):
    _request(issues, 1, "done", started=40, finished=20)   # 20m
    _request(issues, 2, "done", started=70, finished=40, host="laptop")
    _request(issues, 3, "running", started=5)
    _beat(store, [3])
    [run] = host_drain.runs()
    assert (run["lane"], run["item"], run["title"]) == ("issue-fix", "issue #3", "bug 3")
    assert run["typical_s"] == pytest.approx(20 * 60, abs=5)
    assert run["elapsed_s"] == pytest.approx(5 * 60, abs=5)
    assert "~15m left" in host_drain.describe(run)


def test_an_offline_lane_runs_nothing(store, issues):
    _request(issues, 3, "running", started=5)
    _beat(store, [3], at=_ago(10))
    assert host_drain.runs() == []


def test_drained_waits_for_a_beat_after_the_pause(store, issues):
    _request(issues, 3, "running", started=5)
    _beat(store, [3])
    host_drain.pause()
    assert not host_drain.drained()
    _beat(store, [], at=_ago(1))   # idle, but beaten before the pause
    assert not host_drain.drained()
    _beat(store, [])
    assert host_drain.drained()


def test_a_verify_run_and_the_reviews_beside_it_are_each_a_run(store, issues):
    for n in (5, 6, 7):
        store.save_pr(_clean_merge_pr(n))
    store.edit_pr(5).record_verify_request("running", queued_at=_ago(30), host="studio")
    store.save_verify_worker({"host": "studio", "pid": 1, "last_beat": _ago(0),
                              "current_pr": 5, "current_reviews": [6, 7], "autohunt": True})
    data.refresh()
    assert sorted((r["lane"], r["item"]) for r in host_drain.runs()) == [
        ("review", "PR #6"), ("review", "PR #7"), ("verify", "PR #5")]
