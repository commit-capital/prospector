# prospector_app/backend/test_action_items.py
"""GET /api/action-items enriches each item from its source Pr model (#194)."""
from fastapi.testclient import TestClient

from prospector_app.backend import app as appmod
from prospector_app.backend import data
from pipeline import model


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
