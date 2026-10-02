"""The search for pull requests that name an issue."""
from __future__ import annotations

from issue_triage import related_prs
from pipeline import gh


def test_each_pull_request_found_says_whether_it_claims_to_fix_the_issue(monkeypatch):
    monkeypatch.undo()  # the suite's stand-in for the search; this test is of the search
    items = [
        {"number": 3, "title": "a", "state": "open", "user": {"login": "x"},
         "pull_request": {}, "body": "Fixes #7."},
        {"number": 2, "title": "b", "state": "open", "user": {"login": "y"},
         "pull_request": {}, "body": "The Codex counterpart is #7; fixes #70."},
        {"number": 1, "title": "c", "state": "closed", "user": {"login": "z"},
         "pull_request": {"merged_at": "t"}, "body": None},
    ]
    monkeypatch.setattr(gh, "gh_json", lambda path: {"items": items})
    found = related_prs.search(7)
    assert found is not None
    assert [(r["number"], r["state"], r["closes"]) for r in found] == [
        (3, "open", True), (2, "open", False), (1, "merged", False)]
