from dataclasses import dataclass

from prospector_app.backend import author_returns, filters, responses

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
    [r] = _run(prs, named={10: {11}})
    assert [f["number"] for f in r["follow_ups"]] == [11]


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
