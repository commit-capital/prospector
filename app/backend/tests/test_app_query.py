# app/backend/test_app_query.py
"""POST /api/prs/query — the one list endpoint. Loaders monkeypatched."""
from fastapi.testclient import TestClient

from app.backend import app as appmod
from app.backend import data
from pipeline import model
from app.backend import service


def test_query_route(monkeypatch):
    rec = model.Pr(None, {
        "pr": 5, "meta": {"title": "t", "author": "al", "state": "open", "draft": False,
                          "head_sha": "h", "url": "u", "created_at": "2026-01-01T00:00:00+00:00",
                          "updated_at": "2026-01-01T00:00:00+00:00"},
        "signals": {"greptile": 2, "ci": "failing", "mergeable": True,
                    "diffstat": {"additions": 1, "deletions": 0, "changed_files": 1}},
        "drift": {"state": "applicable"}})
    monkeypatch.setattr(data, "prs", lambda: {5: rec})
    monkeypatch.setattr(data, "clusters", lambda: {})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    c = TestClient(appmod.app)
    r = c.post("/api/prs/query", json={"spec": {"greptile": {"op": "<", "value": 3}}})
    assert r.status_code == 200
    body = r.json()
    assert body["match_ids"] == [5] and body["total"] == 1


def test_query_route_clamps_limit_to_max(monkeypatch):
    """#503: page size "All" sends a very large limit — the route must clamp it
    to MAX_PR_QUERY_LIMIT (high enough to cover the whole open-PR corpus), not
    the old 500 that truncated big filtered result sets."""
    captured = {}

    def fake_query_prs(spec, sort=None, direction=None, offset=0, limit=50):
        captured["limit"] = limit
        return {"items": [], "total": 0, "offset": offset, "limit": limit, "match_ids": []}

    monkeypatch.setattr(appmod.service, "query_prs", fake_query_prs)
    c = TestClient(appmod.app)
    r = c.post("/api/prs/query", json={"spec": {}, "limit": 999_999})
    assert r.status_code == 200
    assert captured["limit"] == appmod.MAX_PR_QUERY_LIMIT


def test_old_routes_gone():
    # The old list lanes are unregistered. We check the route table rather than a
    # 404: when a built SPA is present, the catch-all serves index.html (200) for
    # any unmatched /api path, so HTTP status can't distinguish "gone" from "SPA
    # fallback". An absent route is the real invariant.
    paths = {getattr(r, "path", None) for r in appmod.app.routes}
    assert "/api/prs" not in paths
    assert "/api/easy" not in paths
    assert "/api/stale" not in paths


def test_query_row_carries_author_stats_and_trimmed_summary(monkeypatch):
    rec = model.Pr(None, {
        "pr": 7, "meta": {"title": "t", "author": "al", "state": "open", "draft": False,
                          "head_sha": "h", "url": "u", "created_at": "2026-01-01T00:00:00+00:00",
                          "updated_at": "2026-01-01T00:00:00+00:00"},
        "signals": {"greptile": 5, "ci": "passing", "mergeable": True,
                    "diffstat": {"additions": 3, "deletions": 1, "changed_files": 2}},
        "summary": {"one_liner": "Fixes the thing", "primary_change": "patch foo()",
                    "mechanism": "a long mechanism paragraph", "paths": ["a.py", "b.py"]},
        "drift": {"state": "applicable"}})
    monkeypatch.setattr(data, "prs", lambda: {7: rec})
    monkeypatch.setattr(data, "clusters", lambda: {})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "author_stats",
                        lambda handle: {"handle": "al", "url": "u", "merge_rate": 0.72,
                                        "merge_rate_shrunk": 0.6, "merged": 18, "total": 25, "open": 3})
    c = TestClient(appmod.app)
    r = c.post("/api/prs/query", json={"spec": {}})
    assert r.status_code == 200
    row = r.json()["items"][0]
    assert row["author_stats"]["merge_rate"] == 0.72
    # summary is trimmed to the headline projection — heavy fields dropped
    assert row["summary"] == {"one_liner": "Fixes the thing", "primary_change": "patch foo()"}


def _rec(pr: int, *, title: str = "t", author: str = "al", created: str = "2026-01-01T00:00:00+00:00"):
    return model.Pr(None, {
        "pr": pr, "meta": {"title": title, "author": author, "state": "open", "draft": False,
                           "head_sha": "h", "url": "u", "created_at": created,
                           "updated_at": created},
        "signals": {"greptile": 5, "ci": "passing", "mergeable": True,
                    "diffstat": {"additions": 1, "deletions": 0, "changed_files": 1}},
        "drift": {"state": "applicable"}})


def test_every_explorer_column_has_a_sort_key():
    """#180: every column the PR Explorer renders must be server-sortable —
    guards against a column being added to the UI with no backend sort key."""
    explorer_columns = {
        "pr", "loc", "files", "title", "author", "updated", "cluster",
        "safety", "disposition", "greptile", "checks", "drift", "merge", "age",
        "author_rate", "summary",
    }
    assert explorer_columns <= set(service._SORT_KEYS), \
        explorer_columns - set(service._SORT_KEYS)


def test_sort_by_title_ascending(monkeypatch):
    monkeypatch.setattr(data, "prs", lambda: {
        1: _rec(1, title="Zebra"), 2: _rec(2, title="apple"), 3: _rec(3, title="Mango")})
    monkeypatch.setattr(data, "clusters", lambda: {})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    out = service.query_prs({}, sort="title")
    assert [r["number"] for r in out["items"]] == [2, 3, 1]  # apple, Mango, Zebra


def test_sort_by_cluster_puts_unclustered_last(monkeypatch):
    monkeypatch.setattr(data, "prs", lambda: {1: _rec(1), 2: _rec(2), 3: _rec(3)})
    monkeypatch.setattr(data, "clusters", lambda: {})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {1: [30], 3: [10]})  # 2 is unclustered
    out = service.query_prs({}, sort="cluster", direction="asc")
    nums = [r["number"] for r in out["items"]]
    assert nums == [3, 1, 2]  # cluster 10, cluster 30, then the unclustered PR


def test_sort_by_age_desc_is_oldest_first(monkeypatch):
    monkeypatch.setattr(data, "prs", lambda: {
        1: _rec(1, created="2026-06-01T00:00:00+00:00"),   # newer
        2: _rec(2, created="2025-01-01T00:00:00+00:00")})  # older
    monkeypatch.setattr(data, "clusters", lambda: {})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    out = service.query_prs({}, sort="age")  # default desc → oldest first
    assert [r["number"] for r in out["items"]] == [2, 1]


def test_pr_greptile_route(monkeypatch):
    monkeypatch.setattr(appmod.greptile, "fetch_greptile_feedback",
                        lambda n: {"score": 0, "body": "Blocking: unhandled None deref in foo()."})
    c = TestClient(appmod.app)
    r = c.get("/api/prs/7/greptile")
    assert r.status_code == 200
    assert r.json() == {"score": 0, "body": "Blocking: unhandled None deref in foo()."}


def test_pr_greptile_route_empty(monkeypatch):
    monkeypatch.setattr(appmod.greptile, "fetch_greptile_feedback", lambda n: None)
    c = TestClient(appmod.app)
    r = c.get("/api/prs/7/greptile")
    assert r.status_code == 200 and r.json() == {}


def test_api_json_survives_lone_surrogates(monkeypatch):
    """GitHub review/comment text can carry unpaired UTF-16 surrogates (json.loads
    keeps "\\ud800" escapes as-is); the response encoder must degrade them instead
    of 500ing the endpoint."""
    monkeypatch.setattr(service, "pr_detail", lambda n: {"greptile": {"body": "regex /[\ud800-\udfff]/ quoted"}})
    c = TestClient(appmod.app)
    r = c.get("/api/prs/800")
    assert r.status_code == 200
    body = r.json()["greptile"]["body"]
    assert body.startswith("regex /[") and body.endswith("quoted")
