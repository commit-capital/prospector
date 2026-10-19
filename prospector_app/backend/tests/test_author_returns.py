from collections.abc import Iterator

import pytest

from issue_triage.issue_store import IssueStore
from pipeline.store import Store
from prospector_app.backend import activity, author_returns, data, filters, issue_data
from prospector_app.backend import issues as issues_mod
from prospector_app.backend import responses, service

REPO = "o/r"
CLOSED = "2026-06-20T10:00:00+00:00"
AFTER = "2026-06-22T00:00:00Z"
BEFORE = "2026-06-01T00:00:00Z"


def _xref(number: int, author: str, typename: str = "PullRequest", repo: str = REPO,
          created: str = AFTER) -> dict:
    return {"__typename": "CrossReferencedEvent", "createdAt": created,
            "source": {"__typename": typename, "number": number, "createdAt": created,
                       "author": {"login": author}, "repository": {"nameWithOwner": repo}}}


def test_follow_ups_are_the_authors_own_cross_references_and_named_numbers():
    comments = [
        {"author": {"login": "alice", "__typename": "User"}, "createdAt": AFTER,
         "bodyText": "Opened a new PR at #15428 and https://github.com/o/r/pull/77"},
        {"author": {"login": "bob", "__typename": "User"}, "createdAt": AFTER, "bodyText": "see #99"},
        {"author": {"login": "alice", "__typename": "User"}, "createdAt": BEFORE,
         "bodyText": "before our close #55"},
    ]
    refs = [_xref(30, "alice", "Issue"), _xref(31, "alice"), _xref(32, "bob"),
            _xref(33, "alice", repo="alice/fork"), _xref(34, "alice", created=BEFORE)]
    sig = responses.classify(CLOSED, "close", state="closed", comments=comments, reopens=[],
                             commit_dates=[], cross_refs=refs, pr_author="alice", repo=REPO)
    assert sig is not None and sig["follow_ups"] == [30, 31, 77, 15428]
    assert (sig["resubmitted"], sig["resubmitted_pr"]) == (True, 31)


@pytest.fixture
def stores(monkeypatch, tmp_path) -> Iterator[tuple[Store, IssueStore]]:
    """Throwaway PR and issue stores behind the app's snapshots: PR 10 closed,
    PR 30 open and issue 40 open by its author after the close, PR 31 open by
    someone else, PR 32 open by the author before the close."""
    store = Store(tmp_path)
    monkeypatch.setattr(data, "_store", store)
    issue_data.set_store_root(tmp_path)
    for n, state, author, created in ((10, "closed", "alice", BEFORE), (30, "open", "alice", AFTER),
                                      (31, "open", "bob", AFTER), (32, "open", "alice", BEFORE),
                                      (11, "open", "carol", BEFORE)):
        store.save_pr({"pr": n, "meta": {"title": f"t{n}", "state": state, "head_sha": "h",
                                         "author": author, "created_at": created}})
    issues = issue_data.store()
    issues.save_issue({"issue": 40, "meta": {"title": "t40", "state": "open", "author": "alice",
                                             "created_at": AFTER, "updated_at": AFTER}})
    yield store, issues
    issue_data.set_store_root(None)


def _signals(**pr: dict) -> dict:
    return {"pr": {int(n.lstrip("_")): sig for n, sig in pr.items()}, "issue": {}}


CLOSE = {"kind": "close", "at": CLOSED, "reason": "stale", "by": "Ada"}


class TestLinks:
    def test_the_authors_later_open_submissions_follow_up_the_close(self, stores):
        out = author_returns.links(_signals(_10={"our_action": CLOSE, "follow_ups": [30, 31, 32, 40]}),
                                   data.prs(), issue_data.issues())
        assert sorted(out["pr"]) == [30] and sorted(out["issue"]) == [40]
        link = out["pr"][30][0]
        assert (link["signal"], link["closed"]["number"], link["reason"], link["closed_by"]) == (
            "follow-up", 10, "stale", "Ada")

    def test_an_open_item_we_closed_is_its_own_reopened_return(self, stores):
        out = author_returns.links(_signals(_11={"our_action": CLOSE, "reopened": True}),
                                   data.prs(), issue_data.issues())
        assert out["pr"][11][0]["signal"] == "reopened"

    def test_only_our_closes_count(self, stores):
        comment = {**CLOSE, "kind": "comment"}
        out = author_returns.links(_signals(_10={"our_action": comment, "follow_ups": [30]}),
                                   data.prs(), issue_data.issues())
        assert out == {"pr": {}, "issue": {}}


class TestRecorded:
    SIGNALS = _signals(_10={"our_action": CLOSE, "follow_ups": [30]})

    def test_record_writes_and_clears_the_link(self, stores):
        author_returns.record(self.SIGNALS)
        link = author_returns.latest(stores[0].load_pr(30))
        assert link is not None and link["closed"]["number"] == 10
        author_returns.record(_signals())
        assert stores[0].load_pr(30).section("author_return") is None

    def test_the_lists_read_the_recorded_link(self, stores):
        author_returns.record(self.SIGNALS)
        rows = service.query_prs({"author_return": "follow-up"})["items"]
        assert [r["number"] for r in rows] == [30]
        assert issues_mod.query_issues(author_return=True)["total"] == 0

    def test_the_activity_tab_groups_by_close_and_honors_its_scope(self, stores):
        author_returns.record(self.SIGNALS)
        prs, issues = data.prs(), issue_data.issues()
        [r] = author_returns.recorded(prs, issues, activity.ActivityScope())
        assert (r["number"], [f["number"] for f in r["follow_ups"]], r["last_at"]) == (10, [30], AFTER)
        assert author_returns.recorded(prs, issues, activity.ActivityScope(operator="Bo")) == []
        assert author_returns.recorded(
            prs, issues, activity.ActivityScope(pr_numbers=frozenset({30}))) == []


def test_filter_matches_signal():
    row = {"author_return": {"signal": "follow-up"}}
    assert filters.matches(row, {"author_return": True})
    assert filters.matches(row, {"author_return": "follow-up"})
    assert not filters.matches(row, {"author_return": "reopened"})
    assert not filters.matches({}, {"author_return": True})
