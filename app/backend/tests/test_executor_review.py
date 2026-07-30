"""submit_review dry-run/executed detail describes the resulting PR state, so
the cockpit toast says what a bounce actually does (PR stays open, review
submitted, store untouched) rather than only previewing the comment."""
from app.backend import data
from app.backend import executor


def _patch(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {1701: [1]})
    monkeypatch.setattr(executor, "_preflight", lambda *a, **k: (True, ""))
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)


def test_request_changes_dry_run_describes_state(monkeypatch):
    _patch(monkeypatch)
    res = executor.submit_review(1701, "request-changes", "👋 remove the secret…",
                                 token=None, dry_run=True)
    assert res["status"] == "dry-run"
    d = res["detail"]
    assert "CHANGES_REQUESTED review" in d
    assert "PR stays open" in d
    assert "nothing written to the store" in d
    assert "comment:" in d  # still previews the body


def test_request_changes_requires_body(monkeypatch):
    _patch(monkeypatch)
    res = executor.submit_review(1701, "request-changes", "  ", token=None, dry_run=True)
    assert res["status"] == "error" and "comment body is required" in res["detail"]
