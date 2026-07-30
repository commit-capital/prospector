"""Undo/reopen also dismisses the bot's standing request-changes review (#70).

A request-changes is a PR *review* (pulls/{n}/reviews), not an issue comment,
so the old reopen left its "here's what to fix" body visible. reopen_pr now
dismisses it too."""
import types

from app.backend import data
from app.backend import executor


def _ok(stdout=""):
    return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_reopen_dry_run_mentions_dismissing_request_changes(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    res = executor.reopen_pr(123, token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert "request-changes" in res["detail"]


def test_reopen_forced_dry_run_without_token(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    # no token + caller asked for live → forced dry-run, never a write
    res = executor.reopen_pr(123, token=None, dry_run=False)
    assert res["status"] == "dry-run"
    assert res["forced"] is True


def test_reopen_live_dismisses_change_request(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(executor, "_bot_comment_ids", lambda n: [111])
    monkeypatch.setattr(executor, "_bot_change_request_ids", lambda n: [456])
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_reflect_state", lambda *a, **k: None)

    calls = []

    def fake_bot_run(argv, token, **kw):
        calls.append(" ".join(argv))
        return _ok()

    monkeypatch.setattr(executor, "bot_run", fake_bot_run)
    res = executor.reopen_pr(123, token="tok_realish", dry_run=False)

    assert res["status"] == "reopened"
    assert "dismissed 1 request-changes review(s)" in res["detail"]
    # it actually issued the dismissal PUT against the review, not an issue comment
    assert any("pulls/123/reviews/456/dismissals" in c for c in calls)
    assert any("issues/comments/111" in c for c in calls)  # still removes the closing comment


def test_reopen_no_change_request_keeps_detail_clean(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(executor, "_bot_comment_ids", lambda n: [111])
    monkeypatch.setattr(executor, "_bot_change_request_ids", lambda n: [])
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_reflect_state", lambda *a, **k: None)
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _ok())
    res = executor.reopen_pr(123, token="tok_realish", dry_run=False)
    assert res["status"] == "reopened"
    assert "request-changes" not in res["detail"]  # nothing to dismiss → no noise
