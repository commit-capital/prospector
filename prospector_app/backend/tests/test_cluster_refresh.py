"""cluster_refresh: the clustering lane's cadence, budget, lease, and health."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import cluster_pass, storekit
from pipeline import store as S
from pipeline.freshness import SECTION_SCHEMA_VERSION
from prospector_app.backend import cluster_refresh, data, lane_health

NOW = storekit.now()
TODAY = NOW[:10]


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    monkeypatch.setattr(lane_health, "open_or_retest", lambda lane: True)
    monkeypatch.setattr(lane_health, "capacity_open", lambda lane: True)
    return st


def _pr(st, n: int, *, summarized: bool = True, clustered: bool = False) -> None:
    head = f"h{n}"
    rec: dict = {"pr": n, "meta": {"title": "t", "author": "al", "state": "open",
                                   "draft": False, "head_sha": head, "checked_at": NOW}}
    if summarized:
        rec["summary"] = {"one_liner": "o", "subsystem": "ui", "checked_at": NOW,
                          "against_head_sha": head,
                          "schema_version": SECTION_SCHEMA_VERSION["summary"]}
    if clustered:
        rec["cluster"] = {"checked_at": NOW, "against_head_sha": head}
    st.save_pr(rec)


def _run(phase: str, attempted: int, *, trigger: str | None = "worker",
         at: str = NOW) -> storekit.PhaseRun:
    raw = {"phase": phase, "started": at, "finished": at, "stats": {"attempted": attempted}}
    if trigger:
        raw["trigger"] = trigger
    return storekit.PhaseRun(phase=phase, started=at, finished=at, raw=raw)


def test_spent_today_counts_only_the_lane_s_rows_from_today():
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    runs = [_run("cluster:summaries", 40), _run("cluster:summaries", 10),
            _run("analyze:commit", 7), _run("cluster:summaries", 99, trigger="control"),
            _run("cluster:summaries", 50, at=yesterday), _run("cluster:assign", 40)]
    assert cluster_refresh.spent_today(runs, TODAY) == (50, 7)


def test_has_work_reads_summaries_clusters_and_analysis(store):
    _pr(store, 1, clustered=True)
    data.refresh()
    assert not cluster_refresh.has_work(data.prs())
    _pr(store, 2, summarized=False)
    data.refresh()
    assert cluster_refresh.has_work(data.prs())


def test_a_summarized_pr_in_no_cluster_is_work(store):
    _pr(store, 1)
    data.refresh()
    assert cluster_refresh.has_work(data.prs())


@pytest.mark.parametrize("rc, expect", [
    (0, "success"), (cluster_pass.EXIT_AGENT_UNAVAILABLE, "trip"),
    (cluster_pass.EXIT_LIMIT, None), (cluster_pass.EXIT_FAULT, "failure"), (1, "failure")])
def test_book_maps_each_exit_to_the_lane_s_health(monkeypatch, rc, expect):
    seen: list[str] = []
    monkeypatch.setattr(lane_health, "note_success", lambda lane: seen.append("success"))
    monkeypatch.setattr(lane_health, "trip_agent_lanes", lambda reason: seen.append("trip"))
    monkeypatch.setattr(lane_health, "note_failure",
                        lambda lane, **kw: seen.append("failure"))
    cluster_refresh.book(rc, "last line")
    assert seen == ([expect] if expect else [])


def test_a_pass_takes_the_smaller_of_its_share_and_the_day_s_rest(store, monkeypatch):
    _pr(store, 1, summarized=False)
    monkeypatch.setenv("TRIAGE_CLUSTER_DAILY_PRS", "60")
    monkeypatch.setenv("TRIAGE_CLUSTER_DAILY_CLUSTERS", "30")
    store.append_run(_run("cluster:summaries", 45).raw)
    store.append_run(_run("analyze:commit", 25).raw)
    data.refresh()
    spawned: list[tuple[int, int]] = []
    monkeypatch.setattr(cluster_refresh, "_spawn",
                        lambda limit, analyze: (spawned.append((limit, analyze)), (0, "ok"))[1])
    monkeypatch.setattr(lane_health, "note_success", lambda lane: None)
    assert cluster_refresh.pass_once() == 0
    assert spawned == [(15, 5)]


def test_a_spent_day_runs_no_pass(store, monkeypatch):
    _pr(store, 1, summarized=False)
    monkeypatch.setenv("TRIAGE_CLUSTER_DAILY_PRS", "10")
    monkeypatch.setenv("TRIAGE_CLUSTER_DAILY_CLUSTERS", "5")
    store.append_run(_run("cluster:summaries", 10).raw)
    store.append_run(_run("analyze:commit", 5).raw)
    data.refresh()
    monkeypatch.setattr(cluster_refresh, "_spawn", lambda *a: pytest.fail("no pass is due"))
    assert cluster_refresh.pass_once() is None


def test_another_machine_s_lease_holds_the_lane(store, monkeypatch):
    _pr(store, 1, summarized=False)
    data.refresh()
    assert store.claim_lease(cluster_refresh.LEASE, host="macbook", seconds=600)
    monkeypatch.setattr(cluster_refresh, "_spawn", lambda *a: pytest.fail("leased elsewhere"))
    assert cluster_refresh.pass_once() is None


def test_the_lease_is_released_after_a_pass(store, monkeypatch):
    _pr(store, 1, summarized=False)
    data.refresh()
    monkeypatch.setattr(cluster_refresh, "_spawn", lambda *a: (0, "ok"))
    monkeypatch.setattr(lane_health, "note_success", lambda lane: None)
    cluster_refresh.pass_once()
    assert store.claim_lease(cluster_refresh.LEASE, host="macbook", seconds=600)


class TestLease:
    def test_one_host_holds_it_until_it_expires(self, tmp_path):
        st = S.Store(tmp_path / "store")
        now = datetime(2026, 10, 2, tzinfo=timezone.utc)
        assert st.claim_lease("job", host="a", seconds=60, now=now)
        assert st.claim_lease("job", host="a", seconds=60, now=now)
        assert not st.claim_lease("job", host="b", seconds=60, now=now + timedelta(seconds=30))
        assert st.claim_lease("job", host="b", seconds=60, now=now + timedelta(seconds=61))

    def test_only_the_holder_releases_it(self, tmp_path):
        st = S.Store(tmp_path / "store")
        now = datetime(2026, 10, 2, tzinfo=timezone.utc)
        assert st.claim_lease("job", host="a", seconds=60, now=now)
        st.release_lease("job", host="b")
        assert not st.claim_lease("job", host="b", seconds=60, now=now)
        st.release_lease("job", host="a")
        assert st.claim_lease("job", host="b", seconds=60, now=now)
