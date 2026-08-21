"""The issue comment-only executor path: post a comment on a GitHub issue as the
configured bot without closing it. Reversible (the bot can delete its own
comment) and shares the issues/comments endpoint with the close paths."""
from prospector_app.backend import executor, models
from pipeline import settings


def test_comment_issue_dry_run_plans_without_writing():
    res = executor.comment_issue(410, models.IssueCommentBody(comment="ping"), token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert res["action"] == "COMMENT_ISSUE"
    assert res["issue"] == 410


def test_comment_issue_refuses_empty():
    res = executor.comment_issue(410, models.IssueCommentBody(comment="  "), token=None, dry_run=True)
    assert res["status"] == "error"
    assert "empty" in res["detail"].lower()


def test_comment_issue_live_posts(monkeypatch):
    recorded = []
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: recorded.append((a, k)))

    class _Ok:
        returncode = 0
        stderr = ""

    calls = []

    def _fake_bot_run(argv, token, **kw):
        calls.append(argv)
        return _Ok()

    monkeypatch.setattr(executor, "bot_run", _fake_bot_run)
    res = executor.comment_issue(410, models.IssueCommentBody(comment="ping"), token="tok", dry_run=False)
    assert res["status"] == "executed"
    assert calls == [["gh", "issue", "comment", "410", "--repo", settings.repo(), "--body", "ping"]]
    assert len(recorded) == 1


def test_comment_issue_live_bot_run_returncode_nonzero(monkeypatch):
    recorded = []
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: recorded.append((a, k)))

    class _Fail:
        returncode = 1
        stderr = "rate limited"

    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _Fail())
    res = executor.comment_issue(410, models.IssueCommentBody(comment="ping"), token="tok", dry_run=False)
    assert res["status"] == "error"
    assert "comment failed" in res["detail"]
    assert recorded, "a failed upstream write must still be logged"


def test_comment_issue_live_bot_run_raises_records_error_not_uncaught(monkeypatch):
    recorded = []
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: recorded.append((a, k)))

    def _raise(argv, token, **kw):
        raise TimeoutError("gh timed out")

    monkeypatch.setattr(executor, "bot_run", _raise)
    res = executor.comment_issue(410, models.IssueCommentBody(comment="ping"), token="tok", dry_run=False)
    assert res["status"] == "error"
    assert res["detail"] == "issue comment failed unexpectedly; see server logs"
    assert recorded, "a raised exception from bot_run must still be logged"
