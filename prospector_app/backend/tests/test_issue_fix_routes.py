"""The issue-fix review routes: queue an action on an issue's fix attempt, or
cancel it."""
from __future__ import annotations

import pytest

from issue_triage.issue_store import IssueStore
from prospector_app.backend import activity, issue_data


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod

    issue_data.set_store_root(tmp_path)
    monkeypatch.setattr(activity, "operator", lambda: {"name": "Op", "email": None,
                                                       "slug": "op"})
    store = IssueStore(tmp_path)
    store.save_issue({"issue": 7, "meta": {"title": "x is wrong", "state": "open",
                                           "updated_at": "2026-09-01T00:00:00Z", "body": "b"}})
    yield TestClient(appmod.app, raise_server_exceptions=False), store
    issue_data.set_store_root(None)


def test_try_to_fix_queues_a_solve_with_the_guidance(client):
    c, store = client
    r = c.post("/api/issues/7/fix", json={"action": "solve", "guidance": "check x"})
    assert r.status_code == 200 and r.json() == {"ok": True, "detail": "solve queued"}
    issue = store.load_issue(7)
    assert issue.fix_request["requested_by"] == "Op" and issue.fix_request["guidance"] == "check x"


def test_an_action_that_does_not_fit_is_a_409_with_the_reason(client):
    c, _ = client
    r = c.post("/api/issues/7/fix", json={"action": "propose"})
    assert r.status_code == 409 and "no fix attempt" in r.json()["detail"]


def test_an_answer_carries_the_chosen_option(client):
    c, store = client
    store.edit_issue(7).record_fix_run({"ending": "fix-disputed", "host": "s", "question": {
        "question": "2 or 3?", "options": [{"label": "A"}, {"label": "B"}]}})
    r = c.post("/api/issues/7/fix", json={"action": "answer", "answer_label": "B"})
    assert r.status_code == 200 and store.load_issue(7).fix_request["answer"] == {"label": "B"}


def test_a_queued_request_is_cancelled(client):
    c, store = client
    c.post("/api/issues/7/fix", json={"action": "solve"})
    assert c.post("/api/issues/7/fix/cancel").status_code == 200
    assert store.load_issue(7).fix_request["status"] == "cancelled"
    assert c.post("/api/issues/7/fix/cancel").status_code == 409
