import json
from pathlib import Path

import pytest

from issue_triage import fetch_issues
from issue_triage.fetch_issues import is_pull_request, normalize_gql, normalize_issue

FIX = Path(__file__).parent / "fixtures" / "raw_issue.json"


def test_normalize_extracts_signals():
    raw = json.loads(FIX.read_text())
    rec = normalize_issue(raw)
    assert rec["number"] == 4242
    assert rec["labels"] == ["bug"]
    assert rec["comments"] == 7
    assert rec["reactions_total"] == 13
    assert rec["thumbs_up"] == 12
    assert rec["author"] == "alice"


def test_is_pull_request_filters_prs():
    assert is_pull_request({"pull_request": {"url": "x"}}) is True
    assert is_pull_request({"pull_request": None}) is False
    assert is_pull_request({}) is False


def _node(**over: object) -> dict:
    """A GraphQL issue node with the fields normalize_gql reads; override per test."""
    node = {
        "number": 4242, "title": "boom", "body": "repro steps",
        "state": "OPEN", "stateReason": None,
        "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-02T00:00:00Z",
        "author": {"login": "alice"},
        "labels": {"nodes": [{"name": "bug"}]},
        "comments": {"totalCount": 7},
        "reactions": {"totalCount": 13},
        "reactionGroups": [
            {"content": "THUMBS_UP", "reactors": {"totalCount": 12}},
            {"content": "HEART", "reactors": {"totalCount": 1}},
        ],
    }
    node.update(over)
    return node


def test_normalize_gql_matches_rest_shape():
    rec = normalize_gql(_node())
    assert rec["number"] == 4242
    assert rec["state"] == "open"  # enum lowercased to match REST
    assert rec["labels"] == ["bug"]
    assert rec["comments"] == 7
    assert rec["reactions_total"] == 13
    assert rec["thumbs_up"] == 12
    assert rec["author"] == "alice"
    assert rec["state_reason"] is None
    # exact key parity with the REST normalizer — ingest is transport-agnostic
    rest = normalize_issue({"number": 1, "reactions": {}, "user": {}})
    assert set(rec) == set(rest)


def test_normalize_gql_tolerates_null_author_and_no_thumbs():
    rec = normalize_gql(_node(author=None, reactionGroups=[]))
    assert rec["author"] == ""
    assert rec["thumbs_up"] == 0


def _page(numbers: list[int], has_next: bool, cursor: str) -> dict:
    return {"repository": {"issues": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": [_node(number=n) for n in numbers]}}}


def test_fetch_all_paginates_by_cursor(monkeypatch):
    pages = [_page([1], True, "c1"), _page([2], False, "c2")]
    calls: list[dict] = []

    def fake(query: str, variables: dict) -> dict:
        calls.append(variables)
        return pages[len(calls) - 1]

    monkeypatch.setattr(fetch_issues, "gh_graphql", fake)
    rows = fetch_issues.fetch_all()
    assert [r["number"] for r in rows] == [1, 2]
    assert "cursor" not in calls[0]        # first page carries no cursor
    assert calls[1]["cursor"] == "c1"      # second page threads the prior endCursor


def test_fetch_all_raises_on_backstop(monkeypatch):
    # hasNextPage never goes false → exhausts max_pages → loud failure, not truncation
    monkeypatch.setattr(fetch_issues, "gh_graphql",
                        lambda q, v: _page([1], True, "c"))
    with pytest.raises(RuntimeError, match="backstop"):
        fetch_issues.fetch_all(max_pages=3)


def test_fetch_all_smoke_cap_stops_before_backstop(monkeypatch):
    # max_issues caps mid-page even though hasNextPage stays true — no raise
    monkeypatch.setattr(fetch_issues, "gh_graphql",
                        lambda q, v: _page(list(range(100)), True, "c"))
    rows = fetch_issues.fetch_all(max_pages=3, max_issues=5)
    assert len(rows) == 5


def test_issues_query_is_read_only():
    q = fetch_issues._ISSUES_QUERY.strip()
    assert q.startswith("query")
    assert "mutation" not in q.lower()
