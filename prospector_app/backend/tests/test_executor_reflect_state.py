"""A successful upstream action reflects its effect on the PR's state — close →
closed, merge → merged, reopen → open — durably into the shared store (meta.state),
so every operator sees it and the app doesn't show a stale 'open' snapshot until
the next sweep/INGEST."""
import types

from prospector_app.backend import data
from prospector_app.backend import executor
from prospector_app.backend import models


def _ok(stdout=""):
    return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _capture_reflect(monkeypatch):
    seen = []
    monkeypatch.setattr(executor, "_reflect_state",
                        lambda n, *, state=None, merged=False: seen.append((n, state, merged)))
    return seen


def test_close_reflects_closed(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(data, "prs", lambda: {123: {"meta": {}}})
    monkeypatch.setattr(executor, "_pr_live", lambda n: None)        # preflight fail-open
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: True)  # skip the comment step
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _ok())
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    seen = _capture_reflect(monkeypatch)

    res = executor.execute_pr(123, models.CloseAction(action="CLOSE"), token="tok_realish", dry_run=False)
    assert res["status"] == "executed"
    assert seen == [(123, "closed", False)]


def test_close_captures_comment_url_as_event_url(monkeypatch):
    """When the close posts a comment, the comment URL gh prints is stored on
    the action so the panel can deep-link to that exact GitHub event."""
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(data, "prs", lambda: {123: {"meta": {}}})
    monkeypatch.setattr(executor, "_pr_live", lambda n: None)
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)   # we post a comment
    monkeypatch.setattr(executor, "_reflect_state", lambda *a, **k: None)
    comment_url = "https://github.com/test-owner/test-repo/pull/123#issuecomment-9"

    def fake_bot_run(argv, token, **kw):
        return _ok(stdout=comment_url if "comment" in argv else "")

    monkeypatch.setattr(executor, "bot_run", fake_bot_run)
    recorded = {}
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: recorded.update(k))

    res = executor.execute_pr(123, models.CloseAction(action="CLOSE"), token="tok_realish", dry_run=False)
    assert res["status"] == "executed"
    assert res["event_url"] == comment_url
    assert recorded["event_url"] == comment_url   # and it lands on the logged event


def test_close_without_fresh_comment_has_no_event_url(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(data, "prs", lambda: {123: {"meta": {}}})
    monkeypatch.setattr(executor, "_pr_live", lambda n: None)
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: True)    # comment already exists
    monkeypatch.setattr(executor, "_reflect_state", lambda *a, **k: None)
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _ok())
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)

    res = executor.execute_pr(123, models.CloseAction(action="CLOSE"), token="tok_realish", dry_run=False)
    assert res["status"] == "executed"
    assert "event_url" not in res   # nothing posted → no-link fallback


def test_request_changes_review_captures_event_url(monkeypatch):
    """A request-changes review carries a body, so it deep-links to the review
    (read back from the reviews API — gh pr review prints no URL)."""
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(executor, "_preflight", lambda *a, **k: (True, ""))
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _ok())
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    review_url = "https://github.com/test-owner/test-repo/pull/123#pullrequestreview-7"
    monkeypatch.setattr(executor, "_latest_bot_review_url", lambda n: review_url)

    res = executor.submit_review(123, "request-changes", "please fix X", token="tok_realish", dry_run=False)
    assert res["status"] == "executed"
    assert res["event_url"] == review_url


def test_dry_run_close_does_not_reflect(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(data, "prs", lambda: {123: {"meta": {}}})
    monkeypatch.setattr(executor, "_pr_live", lambda n: None)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    seen = _capture_reflect(monkeypatch)

    res = executor.execute_pr(123, models.CloseAction(action="CLOSE"), token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert seen == []  # nothing changed upstream, so nothing to reflect


def test_merge_reflects_merged(monkeypatch):
    from pipeline import gates
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(data, "prs", lambda: {123: types.SimpleNamespace(meta={}, head_sha="deadbeef", linked_issues=[])})
    monkeypatch.setattr(executor, "_changed_paths", lambda n: [])
    monkeypatch.setattr(gates, "merge_eligibility", lambda rec, changed_paths=None, override_reason=None: (True, "ok"))
    monkeypatch.setattr(executor, "_preflight", lambda *a, **k: (True, ""))
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok_realish")
    monkeypatch.setattr(executor, "bot_merge_run", lambda argv, token, **kw: _ok())
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    seen = _capture_reflect(monkeypatch)

    res = executor.merge_pr(123, dry_run=False)
    assert res["status"] == "merged"
    assert seen == [(123, "closed", True)]  # merged=True wins; overlay records "merged"


def test_reopen_reflects_open(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(executor, "_bot_comment_ids", lambda n: [])
    monkeypatch.setattr(executor, "_bot_change_request_ids", lambda n: [])
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _ok())
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    seen = _capture_reflect(monkeypatch)

    res = executor.reopen_pr(123, token="tok_realish", dry_run=False)
    assert res["status"] == "reopened"
    assert seen == [(123, "open", False)]


# ── the durable store write (_reflect_state) ──────────────────────────────────

def _seed_pr(store, n=789, state="open"):
    store.create_pr(n, {
        "title": "fix: a bug", "author": "octocat", "state": state,
        "draft": False, "head_sha": "deadbeef", "base": "main",
        "url": f"https://github.com/test-owner/test-repo/pull/{n}",
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-09T00:00:00+00:00",
    })


def test_reflect_state_flips_state_preserving_meta(monkeypatch, tmp_path):
    from pipeline.store import Store
    store = Store(tmp_path)
    _seed_pr(store)
    monkeypatch.setattr(data, "store", lambda: store)
    monkeypatch.setattr(data, "refresh", lambda: None)

    executor._reflect_state(789, state="closed")

    pr = store.load_pr(789)
    assert pr.state == "closed"          # the durable flip every operator sees
    assert pr.title == "fix: a bug"      # rest of meta untouched
    assert pr.author == "octocat"
    assert pr.head_sha == "deadbeef"


def test_reflect_state_noop_when_pr_absent(monkeypatch, tmp_path):
    from pipeline.store import Store
    store = Store(tmp_path)
    monkeypatch.setattr(data, "store", lambda: store)
    monkeypatch.setattr(data, "refresh", lambda: None)
    executor._reflect_state(999, state="closed")  # no row — must not raise
    assert store.load_pr(999) is None


def test_reflect_state_merge_commits_merged(monkeypatch, tmp_path):
    """merged=True writes the durable state as 'merged', not the raw 'closed'."""
    from pipeline.store import Store
    store = Store(tmp_path)
    _seed_pr(store, n=42)
    monkeypatch.setattr(data, "store", lambda: store)
    monkeypatch.setattr(data, "refresh", lambda: None)

    executor._reflect_state(42, state="closed", merged=True)

    assert store.load_pr(42).state == "merged"
