from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from issue_triage.issue_store import IssueStore
from pipeline.store import Store
from prospector_app.backend import activity, author_returns, data, filters, issue_data
from prospector_app.backend import issues as issues_mod
from prospector_app.backend import responses, service

REPO = "o/r"


@dataclass
class Item:
    state: str | None = "open"
    author: str | None = "alice"
    title: str | None = "t"
    url: str | None = None
    created_at: str | None = "2026-01-01T00:00:00Z"
    body: str | None = None


def _ev(at: str, kind: str, **extra) -> dict:
    return {"at": at, "kind": kind, "status": "executed", "dry_run": False, **extra}


CLOSE_PR = _ev("2026-06-20T10:00:00+00:00", "close", pr=10, reason="stale")
CLOSE_ISSUE = _ev("2026-06-20T10:00:00+00:00", "issue-close", issue=20, action="CLOSE_ISSUE_DUP")


def _run(prs, issues=None, events=None, **kw):
    return author_returns.author_returns(prs, issues or {}, events or [CLOSE_PR], repo=REPO, **kw)


def test_reopened_pr():
    [r] = _run({10: Item()})
    assert (r["kind"], r["number"], r["signal"], r["reason"]) == ("pr", 10, "reopened", "stale")


def test_still_closed_without_follow_up_is_not_a_return():
    assert _run({10: Item(state="closed")}) == []


def test_our_reopen_or_merge_after_the_close_wins():
    for kind in ("reopen", "merge"):
        assert _run({10: Item()}, events=[_ev("2026-06-21T00:00:00+00:00", kind, pr=10), CLOSE_PR]) == []


def test_dry_run_close_does_not_count():
    assert _run({10: Item()}, events=[{**CLOSE_PR, "dry_run": True}]) == []


def test_follow_up_pr_linking_the_closed_pr_by_body():
    prs = {10: Item(state="closed"),
           11: Item(created_at="2026-06-22T00:00:00Z", body="Narrower redo of #10 as asked.")}
    [r] = _run(prs)
    assert r["signal"] == "follow-up" and [f["number"] for f in r["follow_ups"]] == [11]
    assert r["last_at"] == "2026-06-22T00:00:00Z"


def test_follow_up_by_url_and_through_pr_bodies():
    prs = {10: Item(state="closed"), 11: Item(created_at="2026-06-22T00:00:00Z")}
    bodies = {11: "see https://github.com/o/r/pull/10"}
    [r] = _run(prs, pr_bodies=lambda ns: {n: bodies.get(n) for n in ns})
    assert [f["number"] for f in r["follow_ups"]] == [11]


def test_follow_up_requires_same_author_later_open_and_exact_number():
    prs = {10: Item(state="closed"),
           11: Item(author="bob", created_at="2026-06-22T00:00:00Z", body="#10"),
           12: Item(created_at="2026-06-19T00:00:00Z", body="#10"),
           13: Item(state="closed", created_at="2026-06-22T00:00:00Z", body="#10"),
           14: Item(created_at="2026-06-22T00:00:00Z", body="#100 and o/r#10x"),
           15: Item(created_at="2026-06-22T00:00:00Z", body="other/repo#10")}
    assert _run(prs) == []


def test_follow_up_named_from_the_closed_pr_side():
    prs = {10: Item(state="closed"), 11: Item(created_at="2026-06-22T00:00:00Z")}
    [r] = _run(prs, named={("pr", 10): {11}})
    assert [f["number"] for f in r["follow_ups"]] == [11]
    assert _run(prs, named={("issue", 10): {11}}) == []


def test_follow_up_named_from_the_closed_issue_side():
    issues = {20: Item(state="closed"), 21: Item(created_at="2026-06-22T00:00:00Z")}
    [r] = _run({}, issues, [CLOSE_ISSUE], named={("issue", 20): {21}})
    assert (r["kind"], r["number"]) == ("issue", 20)
    assert [(f["kind"], f["number"]) for f in r["follow_ups"]] == [("issue", 21)]


def test_compute_reads_issue_mentions_from_the_stored_responses(monkeypatch):
    from prospector_app.backend import activity, data, issue_data
    monkeypatch.setattr(responses, "recorded", lambda: {
        "pr": {}, "issue": {20: {"mentions": [21], "resubmitted_pr": None}}})
    monkeypatch.setattr(data, "prs", lambda: {})
    monkeypatch.setattr(data, "pr_bodies", lambda ns: {})
    monkeypatch.setattr(activity, "all_events", lambda: [CLOSE_ISSUE])
    monkeypatch.setattr(issue_data, "issues", lambda: {
        20: Item(state="closed"), 21: Item(created_at="2026-06-22T00:00:00Z")})
    [r] = author_returns.compute()
    assert [f["number"] for f in r["follow_ups"]] == [21]


def test_issues_reopened_and_followed_up():
    [r] = _run({}, {20: Item()}, [CLOSE_ISSUE])
    assert (r["kind"], r["signal"], r["reason"]) == ("issue", "reopened", "duplicate")
    issues = {20: Item(state="closed")}
    prs = {30: Item(created_at="2026-06-22T00:00:00Z", body="Fixes #20")}
    [r] = _run(prs, issues, [CLOSE_ISSUE])
    assert r["follow_ups"][0]["kind"] == "pr" and r["follow_ups"][0]["number"] == 30


def test_newest_return_first():
    prs = {10: Item(), 11: Item()}
    events = [_ev("2026-06-21T00:00:00+00:00", "close", pr=11), CLOSE_PR]
    assert [r["number"] for r in _run(prs, events=events)] == [11, 10]


def test_classify_records_the_authors_own_mentions():
    comments = [
        {"author": {"login": "alice", "__typename": "User"}, "createdAt": "2026-06-21T00:00:00Z",
         "bodyText": "Opened a new PR at #15428 and https://github.com/o/r/pull/77"},
        {"author": {"login": "bob", "__typename": "User"}, "createdAt": "2026-06-21T00:00:00Z",
         "bodyText": "see #99"},
        {"author": {"login": "alice", "__typename": "User"}, "createdAt": "2026-06-19T00:00:00Z",
         "bodyText": "before our close #55"},
    ]
    sig = responses.classify("2026-06-20T10:00:00+00:00", "close", state="closed",
                             comments=comments, reopens=[], commit_dates=[],
                             pr_author="alice", repo=REPO)
    assert sig is not None and sig["mentions"] == [77, 15428]


def test_filter_matches_signal():
    row = {"author_return": {"signal": "follow-up"}}
    assert filters.matches(row, {"author_return": True})
    assert filters.matches(row, {"author_return": "follow-up"})
    assert not filters.matches(row, {"author_return": "reopened"})
    assert not filters.matches({}, {"author_return": True})


@pytest.fixture
def stores(monkeypatch, tmp_path) -> Iterator[tuple[Store, IssueStore]]:
    """Throwaway PR and issue stores behind the app's snapshots, with PR 10
    closed by Ada and PR 30 its author's follow-up linking it."""
    store = Store(tmp_path)
    monkeypatch.setattr(data, "_store", store)
    issue_data.set_store_root(tmp_path)
    monkeypatch.setattr(activity, "all_events", lambda: [{**CLOSE_PR, "operator": "Ada"}])
    for n, state, created, body in ((10, "closed", "2026-06-01T00:00:00Z", None),
                                    (30, "open", "2026-06-22T00:00:00Z", "Redo of #10")):
        store.save_pr({"pr": n, "meta": {"title": f"t{n}", "state": state, "head_sha": "h",
                                         "author": "alice", "created_at": created,
                                         "body": body}})
    yield store, issue_data.store()
    issue_data.set_store_root(None)


class TestRecorded:
    def test_record_links_the_follow_up_to_the_close(self, stores):
        author_returns.record()
        link = author_returns.latest(stores[0].load_pr(30))
        assert link is not None and link["signal"] == "follow-up"
        assert (link["closed"]["number"], link["closed_by"]) == (10, "Ada")
        assert stores[0].load_pr(10).section("author_return") is None

    def test_record_clears_a_link_that_no_longer_holds(self, stores, monkeypatch):
        author_returns.record()
        monkeypatch.setattr(activity, "all_events", lambda: [])
        author_returns.record()
        assert stores[0].load_pr(30).section("author_return") is None

    def test_the_lists_read_the_recorded_link(self, stores):
        author_returns.record()
        rows = service.query_prs({"author_return": "follow-up"})["items"]
        assert [r["number"] for r in rows] == [30]
        assert rows[0]["author_return"]["closed"]["number"] == 10
        assert issues_mod.query_issues(author_return=True)["total"] == 0

    def test_the_activity_tab_groups_by_close_and_honors_its_scope(self, stores):
        author_returns.record()
        prs, issues = data.prs(), issue_data.issues()
        [r] = author_returns.recorded(prs, issues, activity.ActivityScope())
        assert (r["number"], [f["number"] for f in r["follow_ups"]]) == (10, [30])
        assert r["last_at"] == "2026-06-22T00:00:00Z"
        assert author_returns.recorded(prs, issues, activity.ActivityScope(operator="Bo")) == []
        assert author_returns.recorded(
            prs, issues, activity.ActivityScope(pr_numbers=frozenset({30}))) == []
