"""The feature-request decision routes and the shortlist the Home card reads."""
from __future__ import annotations

import pytest

from issue_triage.issue_store import IssueStore
from prospector_app.backend import activity, issue_data, issues

SCREEN = {"verdict": "consider", "why": "fits the roadmap", "size": "small", "precedents": []}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod

    issue_data.set_store_root(tmp_path)
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(activity, "operator", lambda: {"name": "Op", "email": None,
                                                       "slug": "op"})
    logged: list[tuple] = []
    monkeypatch.setattr(activity, "record_issue_fix",
                        lambda n, action, detail, **kw: logged.append((n, action, detail)))
    store = IssueStore(tmp_path)
    store.logged = logged  # type: ignore[attr-defined]
    yield TestClient(appmod.app, raise_server_exceptions=False), store
    issue_data.set_store_root(None)


def _issue(store: IssueStore, n: int = 7, *, state: str = "open", labels=("enhancement",),
           screen: dict | None = SCREEN, updated: str = "2026-09-01T00:00:00Z") -> None:
    store.save_issue({"issue": n, "meta": {"title": f"add {n}", "state": state, "body": "b",
                                           "labels": list(labels), "updated_at": updated,
                                           "content_updated_at": updated}})
    if screen is not None:
        store.edit_issue(n).record_feature_screen(screen)


def _card(c) -> list[int]:
    return [r["issue"] for r in c.get("/api/issues/features").json()["items"]]


def test_features_lists_consider_without_decision_ranked_small_first(client):
    c, store = client
    _issue(store, 1, screen={**SCREEN, "size": "large"})
    _issue(store, 2, screen={**SCREEN, "size": "small", "mockup": "<html></html>"})
    _issue(store, 3, screen={**SCREEN, "size": None})
    _issue(store, 4, screen={**SCREEN, "size": "medium"})
    _issue(store, 5, screen={**SCREEN, "verdict": "not-a-fit"})
    _issue(store, 6, state="closed")
    _issue(store, 8, screen=None)
    _issue(store, 9, screen={**SCREEN, "size": "small"})
    store.edit_issue(9).record_feature_decision({"decision": "decline", "by": "m", "at": "t"})
    body = c.get("/api/issues/features").json()
    assert [r["issue"] for r in body["items"]] == [2, 4, 1, 3] and body["count"] == 4
    first = body["items"][0]
    assert first["has_mockup"] is True and first["why"] == "fits the roadmap"
    assert first["title"] == "add 2" and first["url"].endswith("/issues/2")
    assert first["screened_at"]


def test_the_card_reads_a_projection_not_the_issue_table(client, monkeypatch):
    c, store = client
    _issue(store, 2, screen={**SCREEN, "mockup": "<html></html>"})
    _issue(store, 3)
    monkeypatch.setattr(issue_data, "full_issues", lambda: pytest.fail("read every issue"))
    items = c.get("/api/issues/features").json()["items"]
    assert sorted((r["issue"], r["has_mockup"]) for r in items) == [(2, True), (3, False)]


def test_the_snapshot_carries_no_mockup(client):
    c, store = client
    _issue(store, screen={**SCREEN, "mockup": "<html>m</html>"})
    issue_data.refresh()
    assert "mockup" not in issue_data.issues()[7].feature_screen
    assert c.get("/api/issues/7").json()["feature_screen"]["has_mockup"] is True


def test_a_stale_screen_is_off_the_card(client):
    c, store = client
    _issue(store)
    rec = store.load_issue(7).raw
    rec["meta"]["updated_at"] = rec["meta"]["content_updated_at"] = "2026-10-01T00:00:00Z"
    store.save_issue(rec)
    assert _card(c) == []


def test_declined_request_stays_off_card_after_rescreen(client):
    c, store = client
    _issue(store)
    assert c.post("/api/issues/7/feature-decision", json={"decision": "decline"}).status_code == 200
    assert _card(c) == []
    store.edit_issue(7).record_feature_screen(SCREEN)
    issue_data.refresh()
    assert _card(c) == []


def test_implement_queues_feature_solve(client):
    c, store = client
    _issue(store)
    r = c.post("/api/issues/7/feature-decision", json={"decision": "implement",
                                                       "guidance": " keep it small "})
    assert r.status_code == 200
    issue = store.load_issue(7)
    assert issue.fix_request["action"] == "solve" and issue.fix_request["feature"] is True
    assert issue.fix_request["requested_by"] == "Op"
    assert issue.feature_decision["decision"] == "implement"
    assert issue.feature_decision["by"] == "Op" and issue.feature_decision["guidance"] == "keep it small"
    assert _card(c) == []


def test_implement_closed_issue_409_writes_no_decision(client):
    c, store = client
    _issue(store, state="closed")
    r = c.post("/api/issues/7/feature-decision", json={"decision": "implement"})
    assert r.status_code == 409
    assert store.load_issue(7).feature_decision is None and store.logged == []


def test_implement_in_flight_409(client):
    c, store = client
    _issue(store)
    assert c.post("/api/issues/7/fix", json={"action": "solve"}).status_code == 200
    r = c.post("/api/issues/7/feature-decision", json={"decision": "implement"})
    assert r.status_code == 409 and "already" in r.json()["detail"]
    assert store.load_issue(7).feature_decision is None
    assert "feature" not in store.load_issue(7).fix_request


def test_implement_works_on_not_a_fit_verdict(client):
    c, store = client
    _issue(store, screen={**SCREEN, "verdict": "not-a-fit"})
    assert c.post("/api/issues/7/feature-decision", json={"decision": "implement"}).status_code == 200
    assert store.load_issue(7).feature_decision["decision"] == "implement"


def test_reopen_clears_decline(client):
    c, store = client
    _issue(store)
    c.post("/api/issues/7/feature-decision", json={"decision": "decline"})
    assert _card(c) == []
    assert c.post("/api/issues/7/feature-decision", json={"decision": "reopen"}).status_code == 200
    assert store.load_issue(7).feature_decision is None
    assert store.load_issue(7).feature_screen["verdict"] == "consider"
    assert _card(c) == [7]


def test_decision_on_defect_issue_409(client):
    c, store = client
    _issue(store, labels=("bug",), screen=None)
    for d in ("implement", "decline", "reopen"):
        assert c.post("/api/issues/7/feature-decision", json={"decision": d}).status_code == 409
    assert store.load_issue(7).feature_decision is None and store.load_issue(7).fix_request is None


def test_decision_on_unknown_issue_404(client):
    c, _ = client
    assert c.post("/api/issues/99/feature-decision", json={"decision": "decline"}).status_code == 404


def test_feature_mockup_404_without_mockup(client):
    c, store = client
    _issue(store)
    assert c.get("/api/issues/7/feature-mockup").status_code == 404
    assert c.get("/api/issues/99/feature-mockup").status_code == 404


def test_feature_mockup_returns_html_and_detail_omits_it(client):
    c, store = client
    _issue(store, screen={**SCREEN, "mockup": "<html>m</html>"})
    assert c.get("/api/issues/7/feature-mockup").json() == {"html": "<html>m</html>"}
    d = c.get("/api/issues/7").json()
    assert d["feature_screen"]["has_mockup"] is True and "mockup" not in d["feature_screen"]
    assert d["feature_decision"] is None


def test_detail_marks_a_feature_request_whether_or_not_it_was_screened(client):
    c, store = client
    _issue(store, 7, screen=None)
    _issue(store, 8, labels=("bug",), screen=None)
    d = c.get("/api/issues/7").json()
    assert d["is_feature_request"] is True and d["feature_screen"] is None
    assert c.get("/api/issues/8").json()["is_feature_request"] is False


def test_decisions_logged_to_activity(client):
    c, store = client
    _issue(store)
    c.post("/api/issues/7/feature-decision", json={"decision": "decline"})
    c.post("/api/issues/7/feature-decision", json={"decision": "reopen"})
    c.post("/api/issues/7/feature-decision", json={"decision": "implement"})
    assert [(n, a) for n, a, _ in store.logged] == [
        (7, "feature-decline"), (7, "feature-reopen"), (7, "feature-implement")]


def test_the_features_route_is_not_read_as_an_issue_number(client):
    c, _ = client
    assert c.get("/api/issues/features").json() == {"items": [], "count": 0}

