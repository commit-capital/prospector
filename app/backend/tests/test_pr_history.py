"""Condensed on-PR activity history: comments, reviews (Greptile's scored ones
flagged), commits, and reopen/close/force-push/rename events, oldest first.
The GraphQL fetch (`run`) is monkeypatched; no real `gh` calls."""
import json
import types

from app.backend import pr_history


def _fake_run(payload, *, returncode=0):
    return lambda argv, *, timeout=60, **kw: types.SimpleNamespace(
        returncode=returncode, stdout=json.dumps(payload))


def _node(**overrides):
    node = {
        "comments": {"nodes": []},
        "reviews": {"nodes": []},
        "commits": {"nodes": []},
        "timelineItems": {"nodes": []},
    }
    node.update(overrides)
    return node


def _payload(node):
    return {"data": {"repository": {"pullRequest": node}}}


def test_orders_mixed_events_oldest_first(monkeypatch):
    node = _node(
        comments={"nodes": [{"author": {"login": "alice"}, "createdAt": "2026-01-03T00:00:00Z",
                              "url": "https://x/c1", "bodyText": "looks good"}]},
        commits={"nodes": [{"commit": {"oid": "abc1234", "committedDate": "2026-01-01T00:00:00Z",
                                        "message": "fix bug\n\nmore detail", "author": {"name": "bob"}}}]},
        reviews={"nodes": [{"author": {"login": "carol"}, "state": "CHANGES_REQUESTED",
                             "submittedAt": "2026-01-02T00:00:00Z", "url": "https://x/r1",
                             "bodyText": "needs work"}]},
    )
    monkeypatch.setattr(pr_history, "run", _fake_run(_payload(node)))
    items = pr_history.fetch_pr_history(1)
    assert [it["kind"] for it in items] == ["commit", "review", "comment"]
    assert [it["at"] for it in items] == sorted(it["at"] for it in items)


def test_commit_summary_is_first_line_of_message_with_url(monkeypatch):
    node = _node(commits={"nodes": [{"commit": {
        "oid": "deadbeef1234", "committedDate": "2026-01-01T00:00:00Z",
        "message": "fix bug\n\nlonger body here", "author": {"name": "bob"}}}]})
    monkeypatch.setattr(pr_history, "run", _fake_run(_payload(node)))
    item = pr_history.fetch_pr_history(1)[0]
    assert item["kind"] == "commit"
    assert item["summary"] == "fix bug"
    assert item["actor"] == "bob"
    assert item["url"] == "https://github.com/test-owner/test-repo/commit/deadbeef1234"


def test_greptile_review_flagged_with_parsed_score(monkeypatch):
    node = _node(reviews={"nodes": [{
        "author": {"login": "greptile-apps[bot]"}, "state": "COMMENTED",
        "submittedAt": "2026-01-01T00:00:00Z", "url": "https://x/r1",
        "bodyText": "Confidence Score: 4/5\nLooks solid.",
    }]})
    monkeypatch.setattr(pr_history, "run", _fake_run(_payload(node)))
    item = pr_history.fetch_pr_history(1)[0]
    assert item["kind"] == "greptile_review"
    assert item["greptile_score"] == 4


def test_non_greptile_review_has_no_score(monkeypatch):
    node = _node(reviews={"nodes": [{
        "author": {"login": "carol"}, "state": "APPROVED",
        "submittedAt": "2026-01-01T00:00:00Z", "url": "https://x/r1",
        "bodyText": "Confidence Score: 4/5 (quoting someone else)",
    }]})
    monkeypatch.setattr(pr_history, "run", _fake_run(_payload(node)))
    item = pr_history.fetch_pr_history(1)[0]
    assert item["kind"] == "review"
    assert item["greptile_score"] is None
    assert item["state"] == "approved"


def test_renamed_event_summarizes_title_change(monkeypatch):
    node = _node(timelineItems={"nodes": [{
        "__typename": "RenamedTitleEvent", "createdAt": "2026-01-01T00:00:00Z",
        "actor": {"login": "alice"}, "previousTitle": "wip", "currentTitle": "Fix the bug",
    }]})
    monkeypatch.setattr(pr_history, "run", _fake_run(_payload(node)))
    item = pr_history.fetch_pr_history(1)[0]
    assert item["kind"] == "renamed"
    assert item["summary"] == 'renamed "wip" → "Fix the bug"'


def test_reopened_and_force_push_events(monkeypatch):
    node = _node(timelineItems={"nodes": [
        {"__typename": "ReopenedEvent", "createdAt": "2026-01-01T00:00:00Z", "actor": {"login": "alice"}},
        {"__typename": "HeadRefForcePushedEvent", "createdAt": "2026-01-02T00:00:00Z", "actor": {"login": "alice"}},
    ]})
    monkeypatch.setattr(pr_history, "run", _fake_run(_payload(node)))
    items = pr_history.fetch_pr_history(1)
    assert [it["kind"] for it in items] == ["reopened", "force_push"]


def test_items_missing_timestamp_are_dropped(monkeypatch):
    node = _node(comments={"nodes": [{"author": {"login": "alice"}, "createdAt": None,
                                       "url": "https://x/c1", "bodyText": "hi"}]})
    monkeypatch.setattr(pr_history, "run", _fake_run(_payload(node)))
    assert pr_history.fetch_pr_history(1) == []


def test_empty_when_gh_fails(monkeypatch):
    monkeypatch.setattr(pr_history, "run", _fake_run({}, returncode=1))
    assert pr_history.fetch_pr_history(1) == []


def test_empty_when_pr_not_found(monkeypatch):
    monkeypatch.setattr(pr_history, "run", _fake_run({"data": {"repository": {"pullRequest": None}}}))
    assert pr_history.fetch_pr_history(1) == []
