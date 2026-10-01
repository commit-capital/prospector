"""Every executor write lands in the activity log, and the log never turns a
landed write into a raised error."""
import types

import pytest

from prospector_app.backend import data
from prospector_app.backend import executor


def _ok(stdout=""):
    return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


@pytest.fixture
def recorded(monkeypatch):
    entries = []
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **fields: entries.append((kind, fields)))
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    return entries


@pytest.mark.parametrize("dry_run", [True, False])
def test_reopen_pr_dry_run_is_logged(recorded, dry_run):
    res = executor.reopen_pr(123, token=None, dry_run=dry_run)
    assert res["status"] == "dry-run"
    assert recorded == [("reopen", {**res, "identity": executor.settings.bot_login(),
                                    "dry_run": True})]


def test_reopen_issue_dry_run_is_logged(recorded):
    res = executor.reopen_issue(77, token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert [(k, f["dry_run"], f["issue"]) for k, f in recorded] == [("issue-reopen", True, 77)]


def test_raising_bot_call_is_a_logged_error(recorded, monkeypatch):
    def boom(argv, token, **kw):
        raise RuntimeError("socket closed")
    monkeypatch.setattr(executor, "bot_run", boom)

    res = executor.reopen_pr(123, token="tok_realish", dry_run=False)

    assert res["status"] == "error"
    assert "socket closed" not in res["detail"]  # internals stay in the server log
    assert [(k, f["status"], f["dry_run"]) for k, f in recorded] == [("reopen", "error", False)]


def test_raising_question_post_is_a_logged_error(recorded, monkeypatch, tmp_path):
    from issue_triage import dispute_question, fetch_issues, issue_gates, propose
    monkeypatch.setattr(propose, "load_result",
                        lambda issue: {"report_sha": "r1", "base_sha": "b1", "result": {}})
    monkeypatch.setattr(propose, "result_dir", lambda issue: tmp_path)
    monkeypatch.setattr(fetch_issues, "fetch_issue", lambda issue: {"title": "t", "body": "b"})
    monkeypatch.setattr(issue_gates, "question_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(dispute_question, "draft", lambda *a: {"question": "Which?"})
    monkeypatch.setattr(dispute_question, "render", lambda *a, **k: "body")
    monkeypatch.setattr(dispute_question, "problems", lambda body: [])

    def boom(argv, token, **kw):
        raise RuntimeError("socket closed")
    monkeypatch.setattr(executor, "bot_run", boom)

    res = executor.ask_issue_question(9, token="tok_realish", dry_run=False)

    assert res["status"] == "error"
    assert [(k, f["status"]) for k, f in recorded] == [("issue-question", "error")]


def test_failed_log_write_after_a_landed_write_does_not_raise(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(executor, "_bot_comment_ids", lambda n: [])
    monkeypatch.setattr(executor, "_bot_change_request_ids", lambda n: [])
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _ok())
    reflected = []
    monkeypatch.setattr(executor, "_reflect_state",
                        lambda n, *, state=None, merged=False: reflected.append(state))

    def log_down(kind, **fields):
        raise OSError("activity store unreachable")
    monkeypatch.setattr(executor.activity, "record", log_down)

    res = executor.reopen_pr(123, token="tok_realish", dry_run=False)

    assert res["status"] == "reopened"
    assert reflected == ["open"]
    assert res["bookkeeping_error"]


def test_failed_store_reflect_after_a_landed_write_is_still_logged(recorded, monkeypatch):
    monkeypatch.setattr(executor, "_bot_comment_ids", lambda n: [])
    monkeypatch.setattr(executor, "_bot_change_request_ids", lambda n: [])
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _ok())

    def reflect_down(n, *, state=None, merged=False):
        raise OSError("store unreachable")
    monkeypatch.setattr(executor, "_reflect_state", reflect_down)

    res = executor.reopen_pr(123, token="tok_realish", dry_run=False)

    assert res["status"] == "reopened"
    assert res["bookkeeping_error"]
    assert [(k, f["status"]) for k, f in recorded] == [("reopen", "reopened")]
