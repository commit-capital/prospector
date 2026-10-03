from __future__ import annotations

import time

import pytest

from issue_triage import issue_store
from pipeline import storekit
from pipeline.store import LIGHT_CLIP_CHARS, Store
from pipeline.testsupport import greptile_entry, reviews_section
from prospector_app.backend import data, issue_data, run_ledger, snapshot_cache

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
    asked: list[tuple[int | None, list[int]]] = []
    real = store.runs_after
    monkeypatch.setattr(store, "runs_after", lambda rowid, held=(): asked.append(
        (rowid, sorted(held))) or real(rowid, held))
    store.append_run(_run("cluster", "2026-06-02T00:00:00+00:00"))
    assert [r.phase for r in data.runs()] == ["ingest", "cluster"]
    assert asked == [(1 - run_ledger.OVERLAP, [1])]


def test_the_newest_run_of_a_phase_is_read_alone(store):
    assert store.latest_run("ingest") is None
    for phase, at in (("ingest", "2026-06-01T00:00:00+00:00"),
                      ("ingest", "2026-06-03T00:00:00+00:00"),
                      ("cluster", "2026-06-04T00:00:00+00:00")):
        store.append_run(_run(phase, at))
    assert store.latest_run("ingest").finished == "2026-06-03T00:00:00+00:00"


def _ledger_copy() -> snapshot_cache.LedgerFile[storekit.RunRecord]:
    return snapshot_cache.runs_file(data.LEDGER_CACHE_NAME, data._store_key())


def _join_copier(ledger: run_ledger.RunLedger | None) -> None:
    writer = ledger._copier if ledger is not None else None
    if writer is not None:
        writer.join(5)


def _restart_ledger(monkeypatch) -> None:
    _join_copier(data._runs)
    monkeypatch.setattr(data, "_runs", None)


def test_a_restart_seeds_the_ledger_from_its_disk_copy_and_reads_the_rest(store, tmp_path,
                                                                         monkeypatch):
    store.append_run({**_run("ingest", "2026-06-01T00:00:00+00:00"),
                      "ts": "2026-06-01T00:00:00+00:00"})
    store.append_run({**_run("cluster", "2026-06-02T00:00:00+00:00"),
                      "ts": "2026-06-02T00:00:00+00:00"})
    data.runs()
    _restart_ledger(monkeypatch)
    held = _ledger_copy().load()
    assert [(r.rowid, r.ts) for r in held] == [(1, "2026-06-01T00:00:00+00:00"),
                                               (2, "2026-06-02T00:00:00+00:00")]
    (path,) = (tmp_path / "cache").glob(f"{data.LEDGER_CACHE_NAME}-*.json")
    assert path.stat().st_mode & 0o777 == 0o600

    store.append_run({**_run("analyze", "2026-06-03T00:00:00+00:00"),
                      "ts": "2026-06-03T00:00:00+00:00"})
    asked: list[tuple[int | None, list[int]]] = []
    real = store.runs_after
    monkeypatch.setattr(store, "runs_after", lambda rowid, held=(): asked.append(
        (rowid, sorted(held))) or real(rowid, held))
    assert [r.phase for r in data.runs()] == ["ingest", "cluster", "analyze"]
    assert asked == [(2 - run_ledger.OVERLAP, [1])]
    assert ([r.phase for r in data.runs(since="2026-06-02T00:00:00+00:00")]
            == [r.phase for r in store.runs(since="2026-06-02T00:00:00+00:00")]
            == ["cluster", "analyze"])


def test_a_copy_from_a_store_reseeded_at_the_same_address_is_discarded(store, tmp_path,
                                                                       monkeypatch):
    for phase in ("ingest", "cluster", "analyze"):
        store.append_run(_run(phase, "2026-06-01T00:00:00+00:00"))
    data.runs()
    _restart_ledger(monkeypatch)
    assert len(_ledger_copy().load()) == 3

    with store.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM runs")
    for phase, ts in (("security", "2026-07-01T00:00:00+00:00"),
                      ("verify", "2026-07-02T00:00:00+00:00"),
                      ("security", "2026-07-03T00:00:00+00:00")):
        store.append_run({**_run(phase, ts), "ts": ts})
    assert [r.rowid for r in store.runs_after(None)] == [1, 2, 3]

    assert [r.phase for r in data.runs()] == ["security", "verify", "security"]
    _restart_ledger(monkeypatch)
    assert [r.record.phase for r in _ledger_copy().load()] == ["security", "verify", "security"]


def test_an_unreadable_ledger_copy_is_a_miss(store, tmp_path, monkeypatch):
    store.append_run(_run("ingest", "2026-06-01T00:00:00+00:00"))
    data.runs()
    _restart_ledger(monkeypatch)
    (path,) = (tmp_path / "cache").glob(f"{data.LEDGER_CACHE_NAME}-*.json")
    for broken in ("{not json", '{"rows": [[2, null, {"phase": "b"}], [1, null, {"phase": "a"}]]}',
                   '{"rows": [[1, 5, {"phase": "a"}]]}', '{"rows": [[1, null, {}]]}', "[]"):
        path.write_text(broken)
        assert _ledger_copy().load() is None
    asked: list[int | None] = []
    real = store.runs_after
    monkeypatch.setattr(store, "runs_after", lambda rowid, held=(): asked.append(rowid)
                        or real(rowid, held))
    assert [r.phase for r in data.runs()] == ["ingest"]
    assert asked == [None]


def test_no_ledger_copy_under_pytest_without_a_cache_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PROSPECTOR_CACHE_DIR", raising=False)
    copy = snapshot_cache.runs_file(data.LEDGER_CACHE_NAME, "sqlite://")
    assert copy.load() is None
    assert copy.save([storekit.LedgerRow(1, None, storekit.parse_run(_run("ingest", NOW)))]) is False


@pytest.mark.parametrize("family", ["issue", "alert"])
def test_the_issue_and_alert_ledgers_restart_from_their_disk_copies(family, tmp_path,
                                                                    monkeypatch):
    from alert_triage.alert_store import AlertStore
    from prospector_app.backend import alert_data
    module, cls = ((issue_data, issue_store.IssueStore) if family == "issue"
                   else (alert_data, AlertStore))
    monkeypatch.setenv("PROSPECTOR_CACHE_DIR", str(tmp_path / "cache"))
    module.set_store_root(tmp_path / family)
    try:
        module.store().append_run(_run("ingest", "2026-06-01T00:00:00+00:00"))
        module.store().append_run(_run("analyze", "2026-06-02T00:00:00+00:00"))
        assert [r.phase for r in module.runs()] == ["ingest", "analyze"]
        _join_copier(module._state.runs_ledger)
        (path,) = (tmp_path / "cache").glob(f"{module.LEDGER_CACHE_NAME}-*.json")
        assert path.stat().st_mode & 0o777 == 0o600

        module._state.reset()
        module._runs_snapshot.invalidate()
        module.store().append_run(_run("ingest", "2026-06-03T00:00:00+00:00"))
        asked: list[tuple[int | None, list[int]]] = []
        real = cls.runs_after
        monkeypatch.setattr(cls, "runs_after", lambda self, rowid, held=(): asked.append(
            (rowid, sorted(held))) or real(self, rowid, held))
        assert [r.phase for r in module.runs()] == ["ingest", "analyze", "ingest"]
        assert asked == [(2 - run_ledger.OVERLAP, [1])]
    finally:
        module.set_store_root(None)


def test_each_ledger_copies_under_its_own_name():
    """The three ledgers share one store URL in a deployment, so the name is
    what keeps their copies apart."""
    from prospector_app.backend import alert_data
    names = {data.LEDGER_CACHE_NAME, issue_data.LEDGER_CACHE_NAME, alert_data.LEDGER_CACHE_NAME}
    assert len(names) == 3
    assert data.CACHE_NAME not in names and issue_data.CACHE_NAME not in names
