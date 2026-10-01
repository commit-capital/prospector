from __future__ import annotations

import time

import pytest

from issue_triage import issue_store
from pipeline import storekit
from pipeline.store import LIGHT_CLIP_CHARS, Store
from pipeline.testsupport import greptile_entry, reviews_section
from prospector_app.backend import data, issue_data, snapshot_cache

NOW = "2026-06-10T00:00:00+00:00"


def _record(n: int, title: str = "t") -> dict:
    greptile = {**greptile_entry(3, "h1"), "summary": "s" * 1000}
    return {"pr": n,
            "meta": {"title": f"{title}{n}", "author": "a", "state": "open", "draft": False,
                     "head_sha": "h1", "checked_at": NOW, "body": "the description"},
            "reviews": reviews_section("h1", NOW, greptile=greptile)}


def _summary(rec) -> str:
    return rec.review_entry("greptile")["summary"]


@pytest.fixture
def store(tmp_path, monkeypatch) -> Store:
    monkeypatch.setenv("PROSPECTOR_CACHE_DIR", str(tmp_path / "cache"))
    s = Store(tmp_path / "store")
    for n in (1, 2, 3):
        s.save_pr(_record(n))
    _point_data_at(monkeypatch, s)
    return s


def _point_data_at(monkeypatch, s: Store) -> None:
    monkeypatch.setattr(data, "_store", s)
    monkeypatch.setattr(data, "_prs", {})
    monkeypatch.setattr(data, "_clusters", {})
    monkeypatch.setattr(data, "_pr_to_clusters_idx", {})
    monkeypatch.setattr(data, "_pr_watermark", None)
    monkeypatch.setattr(data, "_clu_watermark", None)
    monkeypatch.setattr(data, "_loaded", False)
    monkeypatch.setattr(data, "_light", {})
    monkeypatch.setattr(data, "_cache_written", 0.0)
    monkeypatch.setattr(data, "_cache_generation", None)


def _wait_whole() -> None:
    deadline = time.monotonic() + 5
    while data.light_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert data.light_count() == 0


def test_a_cold_load_publishes_light_records_then_restores_them(store, monkeypatch):
    restore = data._restore_light
    held: list[object] = []
    monkeypatch.setattr(data, "_restore_light", lambda: held.append(1))
    prs = data.prs()
    assert held and data.light_count() == 3
    assert len(_summary(prs[1])) == LIGHT_CLIP_CHARS
    assert _summary(data.pr_whole(1)) == "s" * 1000
    assert data.pr_whole(1).body == "the description"

    restore()
    assert data.light_count() == 0
    assert _summary(data.prs()[1]) == "s" * 1000
    assert not storekit.holds_clipped(store.pr_records(data.prs()))


def test_a_freshen_replaces_a_changed_light_record(store, monkeypatch):
    monkeypatch.setattr(data, "_restore_light", lambda: None)
    data.prs()
    store.save_pr(_record(2, title="edited"))
    data._freshen()
    assert data.light_count() == 2
    assert _summary(data.prs()[2]) == "s" * 1000
    assert data.prs()[2].title == "edited2"


def test_a_restart_reads_the_disk_copy_and_what_changed_since(store, tmp_path, monkeypatch):
    data.prs()
    _wait_whole()
    data._write_cache(force=True)
    assert snapshot_cache.load(data.CACHE_NAME, data._store_key()) is not None

    store.save_pr(_record(4))
    store.save_pr(_record(2, title="edited"))
    store._prs.delete(3)
    _point_data_at(monkeypatch, store)
    monkeypatch.setattr(store, "prs_light", lambda: pytest.fail("read every PR"))

    prs = data.prs()
    assert set(prs) == {1, 2, 4}
    assert prs[2].title == "edited2"
    assert data.light_count() == 0
    assert _summary(prs[1]) == "s" * 1000


def test_a_copy_from_another_store_is_never_read(store, tmp_path):
    data.prs()
    _wait_whole()
    data._write_cache(force=True)
    assert snapshot_cache.load(data.CACHE_NAME, "sqlite:///elsewhere.db") is None


def test_an_unreadable_copy_is_a_miss(store, tmp_path):
    data.prs()
    _wait_whole()
    data._write_cache(force=True)
    for f in (tmp_path / "cache").iterdir():
        f.write_text("{not json")
    assert snapshot_cache.load(data.CACHE_NAME, data._store_key()) is None


def test_no_copy_under_pytest_without_a_cache_dir(monkeypatch):
    monkeypatch.delenv("PROSPECTOR_CACHE_DIR", raising=False)
    assert snapshot_cache.cache_dir() is None
    assert snapshot_cache.save("prs", "sqlite://", None, {1: {}}) is False


def test_the_issue_snapshot_restarts_from_its_disk_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("PROSPECTOR_CACHE_DIR", str(tmp_path / "cache"))
    st = issue_store.IssueStore(tmp_path / "issues")
    for n in (1, 2):
        st.create_issue(n, {"title": f"Bug {n}", "state": "open",
                            "updated_at": "2026-06-23T00:00:00Z"})
    issue_data.set_store_root(tmp_path / "issues")
    try:
        assert set(issue_data.issues()) == {1, 2}
        st.create_issue(3, {"title": "Bug 3", "state": "open",
                            "updated_at": "2026-06-24T00:00:00Z"})
        st._issues.delete(1)
        issue_data._state.reset()
        issue_data._snapshot.invalidate()
        monkeypatch.setattr(issue_store.IssueStore, "issues_since",
                            _refuse_full(issue_store.IssueStore.issues_since))
        assert set(issue_data.issues()) == {2, 3}
    finally:
        issue_data.set_store_root(None)


def _refuse_full(real):
    def since(self, watermark, **kw):
        assert watermark is not None, "read every issue"
        return real(self, watermark, **kw)
    return since


def test_health_answers_while_the_first_load_runs(monkeypatch):
    from fastapi.testclient import TestClient

    from prospector_app.backend import app as app_mod
    monkeypatch.setattr(data, "snapshot_loading", lambda: True)
    monkeypatch.setattr(data, "prs", lambda: pytest.fail("health waited on the snapshot"))
    body = TestClient(app_mod.app).get("/api/health").json()
    assert body["loading"] is True and body["prs"] is None


def _run(phase: str, at: str) -> dict:
    return {"phase": phase, "started": at, "finished": at, "stats": {}}


def test_the_ledger_reads_only_rows_past_the_last_seen(store, monkeypatch):
    store.append_run(_run("ingest", "2026-06-01T00:00:00+00:00"))
    assert [r.phase for r in data.runs()] == ["ingest"]
    asked: list[int | None] = []
    real = store.runs_after
    monkeypatch.setattr(store, "runs_after", lambda rowid: asked.append(rowid) or real(rowid))
    store.append_run(_run("cluster", "2026-06-02T00:00:00+00:00"))
    assert [r.phase for r in data.runs()] == ["ingest", "cluster"]
    assert asked == [1 - data.RUNS_OVERLAP]


def test_the_newest_run_of_a_phase_is_read_alone(store):
    assert store.latest_run("ingest") is None
    for phase, at in (("ingest", "2026-06-01T00:00:00+00:00"),
                      ("ingest", "2026-06-03T00:00:00+00:00"),
                      ("cluster", "2026-06-04T00:00:00+00:00")):
        store.append_run(_run(phase, at))
    assert store.latest_run("ingest").finished == "2026-06-03T00:00:00+00:00"
