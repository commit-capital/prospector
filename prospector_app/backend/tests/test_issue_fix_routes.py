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
    [hold] = c.get("/api/issues/7").json()["fix_holds"]
    assert hold["kind"] == "boundary" and "auth" in hold["text"]


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


def test_going_with_a_pr_backs_it_and_has_the_bot_recommend_it(client, tmp_path, monkeypatch):
    from pipeline.store import Store
    from prospector_app.backend import data, executor
    c, store = client
    prs = Store(tmp_path / "prs")
    prs.save_pr({"pr": 12, "meta": {"title": "t", "state": "open", "author": "contrib",
                                    "head_sha": "a" * 40}})
    monkeypatch.setattr(data, "store", lambda: prs)
    monkeypatch.setattr(data, "refresh", lambda: None)
    posted = []
    monkeypatch.setattr(executor, "post_fix_comment", lambda number, body, **kw: posted.append(
        (number, kw["issue"], kw["dry_run"])) or {"status": "dry-run"})
    r = c.post("/api/issues/7/fix/choose", json={"pr": 12, "reason": "it is better"})
    assert r.status_code == 200 and r.json()["detail"] == "going with #12"
    assert posted == [(12, 7, True)] and prs.load_pr(12).section("backing")["by"] == "Op"
    d = c.get("/api/issues/7").json()
    assert d["fix_status"] == "chosen" and d["fix_choice"]["reason"] == "it is better"
    assert c.post("/api/issues/7/fix/choose", json={"pr": 99}).status_code == 409
    assert c.post("/api/issues/7/fix/unchoose").status_code == 200
    assert c.get("/api/issues/7").json()["fix_choice"] is None
