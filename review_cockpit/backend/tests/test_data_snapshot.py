"""The cockpit reads from an in-memory snapshot that self-freshens incrementally.
These cover the contract data.py relies on: reads serve from memory, the watermark
refetches only what changed (including soft-deletes, which drop from the snapshot),
and a background freshen that errors leaves the last good snapshot in place."""
from __future__ import annotations

import time

from review_cockpit.backend import data
from pipeline import model


def _pr(n: int, state: str = "open"):
    return model.Pr(None, {"pr": n, "meta": {"title": f"p{n}", "state": state,
                                             "head_sha": "h", "url": "u"}})


def _cluster(cid: int):
    return model.Cluster(None, {"id": cid, "root_problem": f"c{cid}", "prs": []})


class _FakeStore:
    """A store whose since() honours the watermark, so a test can assert the reader
    refetches only changed rows. Each row carries the saved_at it reports as the
    high watermark; a deleted cluster is a tombstone the watermark surfaces."""
    def __init__(self):
        self.prs: dict[int, tuple[str, model.Pr]] = {}  # id -> (saved_at, rec)
        self.clusters: dict[int, tuple[str, bool]] = {}  # id -> (saved_at, deleted)
        self.prs_since_calls: list[str | None] = []

    def add(self, n: int, saved_at: str, state: str = "open"):
        self.prs[n] = (saved_at, _pr(n, state))

    def add_cluster(self, cid: int, saved_at: str):
        self.clusters[cid] = (saved_at, False)

    def delete_cluster(self, cid: int, saved_at: str):
        self.clusters[cid] = (saved_at, True)  # tombstone with a fresh watermark

    def prs_since(self, wm):
        self.prs_since_calls.append(wm)
        sel = {n: r for n, (s, r) in self.prs.items() if wm is None or s > wm}
        high = max((s for s, _ in self.prs.values()), default=None)
        return sel, high

    def clusters_since(self, wm):
        changed = {cid: (s, d) for cid, (s, d) in self.clusters.items() if wm is None or s > wm}
        live = {cid: _cluster(cid) for cid, (_, d) in changed.items() if not d}
        deleted = [cid for cid, (_, d) in changed.items() if d]
        high = max((s for s, _ in self.clusters.values()), default=None)
        return live, deleted, high


def test_full_load_then_incremental_uses_watermark(monkeypatch):
    store = _FakeStore()
    store.add(1, "2026-01-01T00:00:00.000000+00:00")
    monkeypatch.setattr(data, "_store", store)

    data._freshen(full=True)
    assert set(data._prs) == {1}
    assert store.prs_since_calls == [None]
    assert data._pr_watermark == "2026-01-01T00:00:00.000000+00:00"

    store.add(2, "2026-01-02T00:00:00.000000+00:00")
    data._freshen()  # incremental
    assert set(data._prs) == {1, 2}
    assert store.prs_since_calls[-1] == "2026-01-01T00:00:00.000000+00:00"  # asked only for >= watermark
    assert data._pr_watermark == "2026-01-02T00:00:00.000000+00:00"


def test_incremental_freshen_drops_deleted_cluster(monkeypatch):
    store = _FakeStore()
    store.add_cluster(1, "2026-01-01T00:00:00.000000+00:00")
    store.add_cluster(2, "2026-01-01T00:00:00.000000+00:00")
    monkeypatch.setattr(data, "_store", store)

    data._freshen(full=True)
    assert set(data._clusters) == {1, 2}

    store.delete_cluster(1, "2026-01-02T00:00:00.000000+00:00")
    data._freshen()  # incremental — the tombstone arrives on the watermark channel
    assert set(data._clusters) == {2}  # removed cluster drops without a full reload
    assert data._clu_watermark == "2026-01-02T00:00:00.000000+00:00"


def test_reads_serve_from_memory_debounced(monkeypatch):
    store = _FakeStore()
    store.add(1, "2026-01-01T00:00:00.000000+00:00")
    monkeypatch.setattr(data, "_store", store)

    assert set(data.prs()) == {1}  # cold load blocks
    data.prs()
    data.prs()
    assert store.prs_since_calls == [None]  # within the debounce window, no refetch


def test_refresh_picks_up_new_writes_incrementally(monkeypatch):
    store = _FakeStore()
    store.add(1, "2026-01-01T00:00:00.000000+00:00")
    monkeypatch.setattr(data, "_store", store)
    data.refresh()
    assert set(data.prs()) == {1}
    assert store.prs_since_calls == [None]  # cold snapshot loads in full

    store.add(2, "2026-01-02T00:00:00.000000+00:00")
    data.refresh()
    assert set(data._prs) == {1, 2}
    assert store.prs_since_calls[-1] == "2026-01-01T00:00:00.000000+00:00"  # incremental, not a full reload


def test_background_freshen_error_keeps_last_snapshot(monkeypatch):
    store = _FakeStore()
    store.add(1, "2026-01-01T00:00:00.000000+00:00")
    monkeypatch.setattr(data, "_store", store)
    data.refresh()
    assert set(data.prs()) == {1}

    def boom(_wm):
        raise RuntimeError("store down")
    monkeypatch.setattr(store, "prs_since", boom)

    data._last_check = 0.0  # let the next read run a check
    data.prs()              # kicks a background freshen that will fail
    for _ in range(100):    # wait for the daemon thread to finish and release the lock
        if not data._check_lock.locked():
            break
        time.sleep(0.02)
    assert set(data._prs) == {1}  # the error was swallowed; the snapshot is intact


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

    row = data.author_stats("al")
    assert row["merged"] == 6          # 1 live merged + 5 baseline
    assert row["open"] == 1
    assert data.author_stats("nobody") is None
    assert data.author_stats(None) is None
