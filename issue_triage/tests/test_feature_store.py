"""The feature-request screen and the maintainers' decision are separate issue
sections; only the screen goes stale when the issue's content moves."""
from __future__ import annotations

from pathlib import Path

from issue_triage import issue_freshness
from issue_triage.issue_model import Issue
from issue_triage.issue_store import IssueStore


def _issue(store: IssueStore, n: int = 7, *, content: str = "2026-10-01T00:00:00Z") -> Issue:
    store.save_issue({"issue": n, "meta": {"title": "add x", "state": "open", "body": "please",
                                           "updated_at": content, "content_updated_at": content}})
    return store.load_issue(n)


def _edited(store: IssueStore, n: int, content: str) -> None:
    """The issue's content moves on GitHub; ingest keeps the sections it holds."""
    rec = store.load_issue(n).raw
    rec["meta"]["updated_at"] = rec["meta"]["content_updated_at"] = content
    store.save_issue(rec)


def test_feature_screen_goes_stale_on_edit(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    _issue(store)
    store.edit_issue(7).record_feature_screen({"verdict": "consider", "why": "w"})
    assert issue_freshness.is_current(store.load_issue(7), "feature_screen")
    _edited(store, 7, "2026-10-02T00:00:00Z")
    assert not issue_freshness.is_current(store.load_issue(7), "feature_screen")


def test_rescreen_keeps_decision(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    _issue(store)
    store.edit_issue(7).record_feature_decision({"decision": "decline", "by": "m", "at": "t"})
    store.edit_issue(7).record_feature_screen({"verdict": "consider", "why": "w"})
    issue = store.load_issue(7)
    assert issue.feature_decision == {"decision": "decline", "by": "m", "at": "t"}
    assert issue.feature_screen["verdict"] == "consider"


def test_decision_never_goes_stale(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    _issue(store)
    store.edit_issue(7).record_feature_decision({"decision": "implement", "by": "m", "at": "t"})
    _edited(store, 7, "2026-10-02T00:00:00Z")
    assert issue_freshness.is_current(store.load_issue(7), "feature_decision")
