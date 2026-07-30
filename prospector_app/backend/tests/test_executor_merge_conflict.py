"""#189: merge_pr refuses a PR that now conflicts upstream, even when the stored
gate was clean — the live pre-flight catches a late conflict before we attempt a
merge that can't succeed."""
from types import SimpleNamespace

from prospector_app.backend import data
from prospector_app.backend import executor
from pipeline import gates


def _setup(monkeypatch, mergeable_state):
    rec = SimpleNamespace(head_sha="h", linked_issues=[])
    monkeypatch.setattr(data, "prs", lambda: {5: rec})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(executor, "_changed_paths", lambda n: [])
    monkeypatch.setattr(gates, "merge_eligibility", lambda rec, **k: (True, "passed all checks run"))
    monkeypatch.setattr(executor, "_pr_live",
                        lambda n: {"state": "open", "merged": False, "head": "h",
                                   "mergeable_state": mergeable_state})
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)


def test_merge_pr_blocks_on_live_conflict(monkeypatch):
    _setup(monkeypatch, "dirty")
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "blocked"
    assert "conflict" in res["detail"].lower()


def test_merge_pr_allows_clean_branch(monkeypatch):
    _setup(monkeypatch, "clean")
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "dry-run"  # no conflict → not blocked
