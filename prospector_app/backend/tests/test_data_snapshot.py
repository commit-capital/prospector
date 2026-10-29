"""The app reads from an in-memory snapshot of the store that freshens
incrementally: reads serve from memory, a refresh picks up new writes and
soft-deleted clusters, a freshen that errors keeps the last good snapshot, and
the cold load runs in the background while readers are told it is loading."""
from __future__ import annotations

import time

import pytest

from pipeline.store import Store
from prospector_app.backend import data


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = Store(tmp_path / "store")
    st.save_pr({"pr": 1, "meta": {"title": "p1", "state": "open", "head_sha": "h", "url": "u"}})
    st.create_cluster(1, "c1")
    st.create_cluster(2, "c2")
    monkeypatch.setattr(data, "_store", st)
    return st


def _settle() -> None:
    for _ in range(100):
        if not data._check_lock.locked():
            return
        time.sleep(0.02)


def test_a_refresh_picks_up_new_writes_and_deleted_clusters(store):
    assert set(data.prs()) == {1} and set(data.clusters()) == {1, 2}

    store.save_pr({"pr": 2, "meta": {"title": "p2", "state": "open", "head_sha": "h", "url": "u"}})
    store.delete_cluster(1)
    data.refresh()

    assert set(data.prs()) == {1, 2}
    assert set(data.clusters()) == {2}


def test_reads_serve_from_memory_until_the_next_check(store):
    assert set(data.prs()) == {1}
    store.save_pr({"pr": 2, "meta": {"title": "p2", "state": "open", "head_sha": "h", "url": "u"}})
    assert set(data.prs()) == {1}


def test_a_freshen_that_errors_keeps_the_last_snapshot(store, monkeypatch):
    assert set(data.prs()) == {1}
    monkeypatch.setattr(store, "prs_since", lambda wm: (_ for _ in ()).throw(
        RuntimeError("store down")))
    data._last_check = 0.0
    data.prs()  # kicks a background freshen that fails
    _settle()
    assert set(data.prs()) == {1}


def test_the_cold_load_runs_in_the_background(store):
    assert data.snapshot_loading()
    for _ in range(100):
        if not data.snapshot_loading():
            break
        time.sleep(0.02)
    assert not data.snapshot_loading()
    assert set(data.prs()) == {1}


def test_author_stats_sums_baseline_and_live_snapshot(tmp_path, monkeypatch):
    from pipeline.store import Store
    store = Store(tmp_path)
    store.save_pr({"pr": 1, "meta": {"title": "t", "author": "al", "state": "open",
                                     "head_sha": "h", "url": "u"}})
    store.save_pr({"pr": 2, "meta": {"title": "t", "author": "al", "state": "merged",
                                     "head_sha": "h", "url": "u"}})
    store.save_author_baseline({"authors": {"al": {"handle": "al", "total": 10, "open": 0,
                               "merged": 5, "closed_unmerged": 5, "comments": 0}},
                               "materialized_at": None})
    monkeypatch.setattr(data, "_store", store)
    monkeypatch.setattr(data, "_loaded", False)
    monkeypatch.setattr(data, "_pr_watermark", None)
    monkeypatch.setattr(data, "_author_table", None)
    monkeypatch.setattr(data, "_author_baseline", None)
    monkeypatch.setattr(data, "_author_issue_generation", None)
    monkeypatch.setattr(data, "_issue_snapshot_generation", lambda: 1)
    monkeypatch.setattr(data, "_issue_author_stats", lambda: {
        "al": {"handle": "al", "url": "https://github.com/al",
               "issues_filed": 3, "issues_resolved": 2},
        "bo": {"handle": "Bo", "url": "https://github.com/Bo",
               "issues_filed": 1, "issues_resolved": 0},
    })

    row = data.author_stats("al")
    assert row["merged"] == 6          # 1 live merged + 5 baseline
    assert row["open"] == 1
    assert row["issues_filed"] == 3
    assert row["issues_resolved"] == 2
    assert data.author_stats("bo") == {
        "handle": "Bo", "url": "https://github.com/Bo",
        "issues_filed": 1, "issues_resolved": 0,
    }
    assert data.author_stats("nobody") is None
    assert data.author_stats(None) is None


def test_a_refresh_recounts_authors_without_rereading_the_baseline(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save_pr({"pr": 1, "meta": {"title": "t", "author": "al", "state": "merged",
                                     "head_sha": "h", "url": "u"}})
    store.save_author_baseline({"authors": {"al": {"handle": "al", "total": 4, "open": 0,
                               "merged": 4, "closed_unmerged": 0, "comments": 0}},
                               "materialized_at": None})
    monkeypatch.setattr(data, "_store", store)
    monkeypatch.setattr(data, "_loaded", False)
    monkeypatch.setattr(data, "_pr_watermark", None)
    monkeypatch.setattr(data, "_author_table", None)
    monkeypatch.setattr(data, "_author_baseline", None)
    monkeypatch.setattr(data, "_issue_snapshot_generation", lambda: 1)
    monkeypatch.setattr(data, "_issue_author_stats", dict)
    assert data.author_stats("al")["merged"] == 5

    monkeypatch.setattr(store, "load_author_baseline", lambda: pytest.fail("baseline re-read"))
    store.save_pr({"pr": 2, "meta": {"title": "t", "author": "al", "state": "merged",
                                     "head_sha": "h", "url": "u"}})
    data.refresh()
    assert data.author_stats("al")["merged"] == 6
