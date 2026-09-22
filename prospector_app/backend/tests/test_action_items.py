# prospector_app/backend/test_action_items.py
"""GET /api/action-items enriches each item from its source Pr model (#194),
serves the list paged with real secret leaks first, and marks fixture-shaped
evidence on read."""
from fastapi.testclient import TestClient

from prospector_app.backend import app as appmod
from prospector_app.backend import data
from pipeline import model


def _item(kind: str, pr: int, **extra) -> dict:
    return {"id": f"{kind}:{pr}", "kind": kind, "pr": pr, "summary": "x",
            "evidence": "", "detail": "", "status": "open",
            "created": "2026-01-01T00:00:00+00:00", **extra}


def test_action_items_enriches_from_pr_model(monkeypatch):
    # data.prs() returns dict[int, Pr] — the endpoint must read the Pr model's
    # typed fields, not treat it as a dict (regression for the .get() crash).
    rec = model.Pr(None, {
        "pr": 4242,
        "meta": {"title": "Rotate the leaked key", "author": "mallory",
                 "url": "https://github.com/x/y/pull/4242"},
        "summary": {"one_liner": "Rotates the secret"}})
    item = {"id": "rotate-secret:4242", "kind": "rotate-secret", "pr": 4242,
            "summary": "rotate", "status": "open", "created": "2026-01-01T00:00:00+00:00"}
    monkeypatch.setattr(data, "prs", lambda: {4242: rec})
    monkeypatch.setattr(data, "action_items", lambda: [item])

    c = TestClient(appmod.app, raise_server_exceptions=False)
    r = c.get("/api/action-items")
    assert r.status_code == 200
    it = r.json()["items"][0]
    assert it["pr_title"] == "Rotate the leaked key"
    assert it["pr_url"] == "https://github.com/x/y/pull/4242"
    assert it["pr_author"] == "mallory"
    assert it["pr_summary"] == "Rotates the secret"


def test_action_items_tolerates_missing_pr(monkeypatch):
    # An action item whose PR isn't in the store must enrich to None, not crash.
    item = {"id": "review:9999", "kind": "review", "pr": 9999,
            "summary": "x", "status": "open", "created": "2026-01-01T00:00:00+00:00"}
    monkeypatch.setattr(data, "prs", lambda: {})
    monkeypatch.setattr(data, "action_items", lambda: [item])

    c = TestClient(appmod.app, raise_server_exceptions=False)
    r = c.get("/api/action-items")
    assert r.status_code == 200
    it = r.json()["items"][0]
    assert it["pr_title"] is None
    assert it["pr_url"] is None
    assert it["pr_author"] is None
    assert it["pr_summary"] is None


def test_real_leaks_sort_first_and_fixtures_last(monkeypatch):
    items = [
        _item("salvage-fix", 1),
        _item("rotate-secret", 2, evidence="tests/fixtures/key.txt: AKIA1234567890ABCDEF"),
        _item("rotate-secret", 3, evidence="src/config.py: AKIA1234567890ABCDEF"),
        _item("review", 4, status="done"),
    ]
    monkeypatch.setattr(data, "prs", lambda: {})
    monkeypatch.setattr(data, "action_items", lambda: items)

    c = TestClient(appmod.app, raise_server_exceptions=False)
    r = c.get("/api/action-items")
    assert r.status_code == 200
    body = r.json()
    got = [(i["id"], i.get("fixture")) for i in body["items"]]
    # real leak, then the non-secret kinds, then the fixture, then non-open
    assert got == [("rotate-secret:3", False), ("salvage-fix:1", None),
                   ("rotate-secret:2", True), ("review:4", None)]
    assert body["total"] == 4


def test_paging_slices_after_ordering_and_counts_the_whole_set(monkeypatch):
    items = [_item("salvage-fix", n) for n in range(1, 8)]
    items.append(_item("rotate-secret", 99, evidence="src/a.py: AKIA1234567890ABCDEF"))
    monkeypatch.setattr(data, "prs", lambda: {})
    monkeypatch.setattr(data, "action_items", lambda: items)

    c = TestClient(appmod.app, raise_server_exceptions=False)
    r = c.get("/api/action-items?limit=3&offset=0")
    body = r.json()
    assert body["total"] == 8
    assert [i["id"] for i in body["items"]] == \
        ["rotate-secret:99", "salvage-fix:1", "salvage-fix:2"]
    r = c.get("/api/action-items?limit=3&offset=6")
    assert [i["id"] for i in r.json()["items"]] == ["salvage-fix:6", "salvage-fix:7"]


def test_stored_fixture_mark_wins_over_the_read_side_guess(monkeypatch):
    # threat_scan stamps `fixture` at emission; the endpoint only fills the key
    # in for items written before the mark existed.
    items = [_item("rotate-secret", 5, evidence="src/config.py: key", fixture=True)]
    monkeypatch.setattr(data, "prs", lambda: {})
    monkeypatch.setattr(data, "action_items", lambda: items)

    c = TestClient(appmod.app, raise_server_exceptions=False)
    r = c.get("/api/action-items")
    it = r.json()["items"][0]
    assert it["fixture"] is True
