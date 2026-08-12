# prospector_app/backend/tests/test_home_counts.py
"""POST /api/prs/counts — batch match totals backing the Home screen's cards."""
import threading
import time

from fastapi.testclient import TestClient

from pipeline import model
from prospector_app.backend import app as appmod
from prospector_app.backend import data
from prospector_app.backend import service


def _rec(pr: int, *, greptile: int = 5, ci: str = "passing") -> model.Pr:
    return model.Pr(None, {
        "pr": pr, "meta": {"title": "t", "author": "al", "state": "open", "draft": False,
                           "head_sha": "h", "url": "u", "created_at": "2026-01-01T00:00:00+00:00",
                           "updated_at": "2026-01-01T00:00:00+00:00"},
        "signals": {"greptile": greptile, "ci": ci, "mergeable": True,
                    "diffstat": {"additions": 1, "deletions": 0, "changed_files": 1}},
        "drift": {"state": "applicable"}})


def _snapshot(monkeypatch, prs: dict[int, model.Pr]) -> None:
    monkeypatch.setattr(data, "_loaded", True)  # counts serve rather than report loading
    monkeypatch.setattr(data, "prs", lambda: prs)
    monkeypatch.setattr(data, "clusters", lambda: {})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})


def test_counts_route_one_total_per_spec_in_order(monkeypatch):
    _snapshot(monkeypatch, {1: _rec(1, greptile=5), 2: _rec(2, greptile=2, ci="failing")})
    c = TestClient(appmod.app)
    r = c.post("/api/prs/counts", json={"specs": [
        {},
        {"greptile": {"op": ">", "value": 4}},
        {"ci": "failing"},
        {"greptile": {"op": ">", "value": 9}},
    ]})
    assert r.status_code == 200
    assert r.json() == {"counts": [2, 1, 1, 0]}


def test_counts_agree_with_query_totals(monkeypatch):
    _snapshot(monkeypatch, {n: _rec(n, greptile=n % 6) for n in range(1, 8)})
    spec = {"greptile": {"op": ">=", "value": 3}}
    c = TestClient(appmod.app)
    r = c.post("/api/prs/counts", json={"specs": [spec]})
    assert r.status_code == 200
    assert r.json()["counts"] == [service.query_prs(spec, limit=0)["total"]]


def test_counts_empty_specs_list(monkeypatch):
    _snapshot(monkeypatch, {1: _rec(1)})
    c = TestClient(appmod.app)
    r = c.post("/api/prs/counts", json={"specs": []})
    assert r.status_code == 200
    assert r.json() == {"counts": []}


def test_counts_report_loading_while_snapshot_cold_loads(monkeypatch):
    """A counts request during the cold load answers immediately with
    loading:true instead of blocking until the snapshot is published."""
    started = threading.Event()
    release = threading.Event()

    def slow_freshen(full: bool = False) -> None:
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(data, "_loaded", False)
    monkeypatch.setattr(data, "_freshen", slow_freshen)
    c = TestClient(appmod.app)
    try:
        r = c.post("/api/prs/counts", json={"specs": [{}]})
        assert r.status_code == 200
        assert r.json() == {"counts": None, "loading": True}
        for _ in range(1000):  # generous headroom for the daemon thread to get scheduled
            if started.is_set():  # under -n auto, every core runs a full worker process,
                break             # so scheduling can lag well past a hardcoded few seconds
            time.sleep(0.02)
        assert started.is_set(), "the endpoint never kicked the load onto a background thread"
    finally:
        release.set()
    for _ in range(100):  # wait for the daemon thread to release the freshen lock
        if not data._check_lock.locked():
            break
        time.sleep(0.02)


def test_counts_serve_once_background_load_publishes(monkeypatch):
    """The poll converges: loading:true while the cold load runs, real counts
    right after it publishes the snapshot."""
    monkeypatch.setattr(data, "_loaded", False)
    monkeypatch.setattr(data, "_freshen", lambda full=False: None)
    monkeypatch.setattr(data, "prs", lambda: {1: _rec(1)})
    monkeypatch.setattr(data, "clusters", lambda: {})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    c = TestClient(appmod.app)
    r = c.post("/api/prs/counts", json={"specs": [{}]})
    assert r.json() == {"counts": None, "loading": True}
    for _ in range(100):  # the instant background load flips _loaded
        if data._loaded:
            break
        time.sleep(0.02)
    r = c.post("/api/prs/counts", json={"specs": [{}]})
    assert r.json() == {"counts": [1]}


def test_counts_rejects_missing_or_non_list_specs():
    c = TestClient(appmod.app)
    assert c.post("/api/prs/counts", json={}).status_code == 422
    assert c.post("/api/prs/counts", json={"specs": {"clean": True}}).status_code == 422


def test_counts_rejects_non_object_spec():
    c = TestClient(appmod.app)
    r = c.post("/api/prs/counts", json={"specs": [{}, "clean"]})
    assert r.status_code == 422


def test_counts_rejects_oversized_batch():
    c = TestClient(appmod.app)
    r = c.post("/api/prs/counts", json={"specs": [{}] * (appmod.MAX_PR_COUNT_SPECS + 1)})
    assert r.status_code == 422
