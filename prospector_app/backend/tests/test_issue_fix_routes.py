"""The issue-fix review routes: queue an action on an issue's fix attempt, or
cancel it."""
from __future__ import annotations

import pytest

from issue_triage.issue_store import IssueStore
from prospector_app.backend import activity, issue_data, issues


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod

    issue_data.set_store_root(tmp_path)
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
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


def test_the_issue_says_why_its_fix_waits_for_an_operator(client):
    c, store = client
    store.edit_issue(7).record_fix_run({"ending": "fixed", "host": "s", "patch": "p",
                                        "boundary": {"crossings": [{
                                            "kind": "auth", "where": "a", "what": "w",
                                            "requested": True}]}})
    assert "auth" in c.get("/api/issues/7").json()["fix_held"]


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


def test_approving_an_open_fix_leaves_it_to_the_maintainers(client):
    c, store = client
    store.edit_issue(7).record_fix_run({"ending": "fixed", "host": "s", "patch": "p",
                                        "proposal": {"pr": 9, "url": "u"}})
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "handed-back",
                                             "judged": {"reason": "CI fails"}})
    assert c.get("/api/issues/7").json()["fix_status"] == "review"
    r = c.post("/api/issues/7/fix/approve", json={"partial": True})
    assert r.status_code == 200 and r.json()["detail"] == "#9 approved"
    d = c.get("/api/issues/7").json()
    assert d["fix_status"] == "pr-open"
    assert d["fix_approved"]["by"] == "Op" and d["fix_approved"]["partial"] is True


def test_approving_without_an_open_pull_request_is_a_409(client):
    c, _ = client
    r = c.post("/api/issues/7/fix/approve", json={"partial": False})
    assert r.status_code == 409 and "no open pull request" in r.json()["detail"]
