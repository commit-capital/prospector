"""Community response classification + the PR-queue widening that surfaces
pushback on PRs we already closed. classify() is pure; scan and ack run
against throwaway stores."""
import json
import logging
import subprocess
import types
from collections.abc import Iterator

import pytest

from issue_triage.issue_store import IssueStore
from pipeline import model
from pipeline.store import Store
from prospector_app.backend import responses
from prospector_app.backend import service
from prospector_app.backend import data
from prospector_app.backend import issue_data
from pipeline import settings

ACT = "2026-06-10T00:00:00+00:00"          # when we acted
BEFORE = "2026-06-09T00:00:00Z"
AFTER = "2026-06-11T00:00:00Z"
LATER = "2026-06-12T00:00:00Z"


def _comment(login, at, body="hi", typename="User"):
    return {"author": {"login": login, "__typename": typename},
            "createdAt": at, "url": "u", "bodyText": body}


def _reopen(login, at, typename="User"):
    return {"__typename": "ReopenedEvent",
            "actor": {"login": login, "__typename": typename}, "createdAt": at}


SAME_REPO = f"{settings.repo_owner()}/{settings.repo_name()}"


def _cross_ref(new_pr_number, new_pr_author, event_at, pr_created_at=None,
               repo=SAME_REPO):
    return {
        "__typename": "CrossReferencedEvent",
        "createdAt": event_at,
        "source": {
            "__typename": "PullRequest",
            "number": new_pr_number,
            "createdAt": pr_created_at or event_at,
            "author": {"login": new_pr_author},
            "repository": {"nameWithOwner": repo},
        },
    }


class TestClassify:
    def test_human_reply_after_action(self):
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[_comment("alice", AFTER, "why closed?")],
                                 reopens=[], commit_dates=[])
        assert sig and sig["replied"] and sig["by"] == "alice"
        assert "why closed" in sig["snippet"]
        assert sig["last_response_at"] == AFTER

    def test_comment_before_action_is_not_a_response(self):
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[_comment("alice", BEFORE)],
                                 reopens=[], commit_dates=[])
        assert sig is None

    def test_bot_comment_ignored(self):
        # The shape GraphQL actually returns: an app's login is bare and its
        # type is Bot. `coderabbitai` and `greptile-apps` reach us exactly so.
        sig = responses.classify(ACT, "comment", state="open",
                                 comments=[_comment(settings.bot_login(), AFTER, typename="Bot"),
                                           _comment("greptile-apps", AFTER, typename="Bot"),
                                           _comment("coderabbitai", AFTER, typename="Bot")],
                                 reopens=[], commit_dates=[])
        assert sig is None

    def test_bot_suffixed_login_ignored_without_a_type(self):
        # REST-shaped payloads name the same accounts `coderabbitai[bot]` and
        # carry no type; the suffix identifies them.
        sig = responses.classify(ACT, "comment", state="open",
                                 comments=[{"login": "coderabbitai[bot]", "createdAt": AFTER,
                                            "bodyText": "walkthrough"}],
                                 reopens=[], commit_dates=[])
        assert sig is None

    def test_a_human_whose_login_merely_contains_bot_still_counts(self):
        sig = responses.classify(ACT, "comment", state="open",
                                 comments=[_comment("botanist", AFTER, body="this still repros")],
                                 reopens=[], commit_dates=[])
        assert sig and sig["replied"] and sig["by"] == "botanist"

    def test_bot_reopen_ignored(self):
        sig = responses.classify(ACT, "comment", state="open", comments=[],
                                 reopens=[_reopen("coderabbitai", AFTER, typename="Bot")],
                                 commit_dates=[])
        assert sig is None

    def test_reopen_event_after_action(self):
        sig = responses.classify(ACT, "close", state="open",
                                 comments=[], reopens=[_reopen("bob", AFTER)], commit_dates=[])
        assert sig and sig["reopened"]

    def test_state_flip_open_after_close_counts_as_reopened(self):
        # No reopen event in the window, but it's open and we closed it.
        sig = responses.classify(ACT, "close", state="open",
                                 comments=[], reopens=[], commit_dates=[])
        assert sig and sig["reopened"]

    def test_our_own_reopen_does_not_count(self):
        # We reopened it (as the bot) and it's open — bot reopen ignored, and a
        # comment/merge-kind action means no close-state-flip either.
        sig = responses.classify(ACT, "reopen", state="open",
                                 comments=[], reopens=[_reopen(settings.bot_login(), AFTER)],
                                 commit_dates=[])
        assert sig is None

    def test_new_commit_after_action(self):
        sig = responses.classify(ACT, "request-changes", state="open",
                                 comments=[], reopens=[], commit_dates=[AFTER])
        assert sig and sig["new_commits"]

    def test_old_commit_only_is_not_a_response(self):
        sig = responses.classify(ACT, "comment", state="open",
                                 comments=[], reopens=[], commit_dates=[BEFORE])
        assert sig is None

    def test_multiple_signals_and_latest_wins(self):
        sig = responses.classify(ACT, "close", state="open",
                                 comments=[_comment("alice", AFTER)],
                                 reopens=[_reopen("bob", LATER)], commit_dates=[AFTER])
        assert sig["replied"] and sig["reopened"] and sig["new_commits"]
        assert sig["last_response_at"] == LATER

    def test_resubmitted_by_same_author_after_close(self):
        ref = _cross_ref(new_pr_number=999, new_pr_author="alice", event_at=AFTER)
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[], reopens=[], commit_dates=[],
                                 cross_refs=[ref], pr_author="alice")
        assert sig and sig["resubmitted"] is True
        assert sig["resubmitted_pr"] == 999

    def test_resubmitted_by_different_author_ignored(self):
        ref = _cross_ref(new_pr_number=999, new_pr_author="bob", event_at=AFTER)
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[], reopens=[], commit_dates=[],
                                 cross_refs=[ref], pr_author="alice")
        assert sig is None

    def test_resubmitted_before_action_is_ignored(self):
        ref = _cross_ref(new_pr_number=999, new_pr_author="alice", event_at=BEFORE)
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[], reopens=[], commit_dates=[],
                                 cross_refs=[ref], pr_author="alice")
        assert sig is None

    def test_resubmitted_without_pr_author_is_ignored(self):
        ref = _cross_ref(new_pr_number=999, new_pr_author="alice", event_at=AFTER)
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[], reopens=[], commit_dates=[],
                                 cross_refs=[ref], pr_author=None)
        assert sig is None

    def test_resubmitted_sets_last_response_at(self):
        ref = _cross_ref(new_pr_number=999, new_pr_author="alice", event_at=LATER)
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[], reopens=[], commit_dates=[],
                                 cross_refs=[ref], pr_author="alice")
        assert sig and sig["last_response_at"] == LATER

    def test_resubmitted_cross_repo_ref_ignored(self):
        # #527: a same-author cross-reference from a *different* repository (e.g.
        # the author's fork of an unrelated project) must not be treated as a
        # resubmission of this PR — its PR number isn't comparable across repos.
        ref = _cross_ref(new_pr_number=75, new_pr_author="alice", event_at=AFTER,
                         repo="alice/some-other-project")
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[], reopens=[], commit_dates=[],
                                 cross_refs=[ref], pr_author="alice")
        assert sig is None

    def test_resubmitted_missing_repository_field_ignored(self):
        # A cross-ref whose source PR carries no repository info (defensive:
        # never assume same-repo when we can't confirm it).
        ref = _cross_ref(new_pr_number=999, new_pr_author="alice", event_at=AFTER)
        ref["source"].pop("repository")
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[], reopens=[], commit_dates=[],
                                 cross_refs=[ref], pr_author="alice")
        assert sig is None

    def test_resubmitted_does_not_set_resubmitted_flag_when_false(self):
        # A plain reply still returns resubmitted=False (not absent, just False).
        sig = responses.classify(ACT, "close", state="closed",
                                 comments=[_comment("alice", AFTER)],
                                 reopens=[], commit_dates=[])
        assert sig is not None
        assert sig["resubmitted"] is False
        assert sig["resubmitted_pr"] is None


def _closed_pr(sig: dict) -> model.Pr:
    return model.Pr(None, {
        "pr": 42, "meta": {"title": "t", "author": "a", "state": "closed",
                           "draft": False, "head_sha": "h", "url": "x",
                           "created_at": ACT, "updated_at": AFTER, "checked_at": ACT},
        "signals": {"checked_at": ACT, "against_head_sha": "h"},
        "drift": {"state": "applicable", "checked_at": ACT, "against_head_sha": "h"},
        "responses": {"pr": 42, **sig}})


class TestQueueWidening:
    def test_responses_filter_surfaces_a_closed_pr(self, monkeypatch):
        # A PR we closed, with a reply — normally excluded (closed), but the
        # responses filter widens to any state.
        rec = _closed_pr({"reopened": False, "new_commits": False, "replied": True,
                          "last_response_at": AFTER, "snippet": "why?", "by": "a"})
        monkeypatch.setattr(data, "prs", lambda: {42: rec})
        monkeypatch.setattr(data, "pr_to_clusters", lambda: {})

        # without the filter the closed PR is absent
        assert service.query_prs({})["total"] == 0
        # with it, the closed PR surfaces, carrying its responses payload
        out = service.query_prs({"responses": "replied"})
        assert out["total"] == 1
        assert out["items"][0]["number"] == 42
        assert out["items"][0]["responses"]["replied"] is True

    def test_responses_filter_excludes_non_matching_signal(self, monkeypatch):
        rec = _closed_pr({"reopened": False, "new_commits": False, "replied": True,
                          "resubmitted": False, "resubmitted_pr": None,
                          "last_response_at": AFTER, "snippet": "why?", "by": "a"})
        monkeypatch.setattr(data, "prs", lambda: {42: rec})
        monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
        # filtering for reopened (which this PR lacks) yields nothing
        assert service.query_prs({"responses": "reopened"})["total"] == 0

    def test_resubmitted_filter_surfaces_closed_pr(self, monkeypatch):
        rec = _closed_pr({"reopened": False, "new_commits": False, "replied": False,
                          "resubmitted": True, "resubmitted_pr": 99,
                          "last_response_at": AFTER, "snippet": None, "by": None})
        monkeypatch.setattr(data, "prs", lambda: {42: rec})
        monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
        # resubmitted filter surfaces the closed PR
        out = service.query_prs({"responses": "resubmitted"})
        assert out["total"] == 1
        assert out["items"][0]["number"] == 42
        assert out["items"][0]["responses"]["resubmitted"] is True
        assert out["items"][0]["responses"]["resubmitted_pr"] == 99
        # filtering for a signal this PR lacks still excludes it
        assert service.query_prs({"responses": "replied"})["total"] == 0


def _fake_run_ok(node):
    return lambda argv, *, timeout=90, **kw: types.SimpleNamespace(
        returncode=0, stdout=json.dumps({"data": {"repository": {"p0": node}}}), stderr="")


class TestFetchResilience:
    """_fetch must not let one bad chunk take down the whole sweep (#537): a
    silent/raised failure on one gh call previously either aborted scan()
    entirely (an uncaught TimeoutExpired) or silently dropped that chunk's PRs
    from the registry with zero visibility."""

    def test_timeout_on_one_chunk_does_not_abort_others(self, monkeypatch, caplog):
        monkeypatch.setattr(responses, "_CHUNK", 1)  # one PR per gh call
        node = {"number": 0, "state": "CLOSED", "author": {"login": "alice"},
               "comments": {"nodes": []}, "timelineItems": {"nodes": []}, "commits": {"nodes": []}}
        calls = []

        def fake_run(argv, *, timeout=90, **kw):
            calls.append(argv)
            if len(calls) == 2:  # the second PR's chunk is wedged
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            return types.SimpleNamespace(returncode=0,
                                         stdout=json.dumps({"data": {"repository": {"p0": node}}}),
                                         stderr="")

        monkeypatch.setattr(responses.gh.subprocess, "run", fake_run)
        with caplog.at_level(logging.WARNING, logger="prospector_app.backend.responses"):
            nodes, failed = responses._fetch([10, 11, 12])
        assert failed == [11]
        assert set(nodes) == {10, 12}
        assert any("timed out" in r.message for r in caplog.records)

    def test_nonzero_returncode_with_no_body_marks_chunk_failed_and_logs(self, monkeypatch, caplog):
        monkeypatch.setattr(responses, "_CHUNK", 1)
        monkeypatch.setattr(responses.gh.subprocess, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=1, stdout="", stderr="rate limited"))
        with caplog.at_level(logging.WARNING, logger="prospector_app.backend.responses"):
            nodes, failed = responses._fetch([5])
        assert nodes == {}
        assert failed == [5]
        assert any("returned no data" in r.message for r in caplog.records)

    def test_one_deleted_pr_does_not_cost_its_chunk_the_rest(self, monkeypatch, caplog):
        # The real shape upstream returns: a PR deleted from the repository makes
        # `gh` exit 1 and its alias null, while every other alias in the same
        # body resolves. PR 6418 was closed as the bot and is now gone; it took
        # the other 19 in its chunk with it.
        node = {"number": 0, "state": "CLOSED", "author": {"login": "alice"},
                "comments": {"nodes": []}, "timelineItems": {"nodes": []},
                "commits": {"nodes": []}}
        body = {
            "data": {"repository": {"p0": None, "p1": node, "p2": node}},
            "errors": [{"type": "NOT_FOUND", "path": ["repository", "p0"],
                        "message": "Could not resolve to a PullRequest with the number of 6418."}],
        }
        monkeypatch.setattr(responses.gh.subprocess, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=1, stdout=json.dumps(body),
                                                  stderr="gh: Could not resolve to a PullRequest "
                                                         "with the number of 6418."))
        with caplog.at_level(logging.WARNING, logger="prospector_app.backend.responses"):
            nodes, failed = responses._fetch([6418, 1969, 66])
        assert set(nodes) == {1969, 66}   # the live PRs survive their chunkmate
        assert failed == []               # NOT_FOUND is an answer, not a failure
        assert any("2 resolved, 1 gone from the repo, 0 unread" in r.message
                   for r in caplog.records)

    def test_an_empty_alias_without_a_not_found_error_is_unread(self, monkeypatch):
        # No NOT_FOUND for p0, so its emptiness is unexplained: treat it as
        # unread and let scan keep whatever it already knew about that PR.
        node = {"number": 0, "state": "CLOSED", "author": {"login": "alice"},
                "comments": {"nodes": []}, "timelineItems": {"nodes": []},
                "commits": {"nodes": []}}
        body = {"data": {"repository": {"p0": None, "p1": node}},
                "errors": [{"type": "RATE_LIMITED", "path": ["repository", "p0"]}]}
        monkeypatch.setattr(responses.gh.subprocess, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=1, stdout=json.dumps(body), stderr=""))
        nodes, failed = responses._fetch([400, 401])
        assert set(nodes) == {401}
        assert failed == [400]

    def test_unparseable_response_marks_chunk_failed(self, monkeypatch):
        monkeypatch.setattr(responses, "_CHUNK", 1)
        monkeypatch.setattr(responses.gh.subprocess, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=0, stdout="not json", stderr=""))
        nodes, failed = responses._fetch([7])
        assert nodes == {}
        assert failed == [7]

    def test_missing_node_in_a_successful_chunk_is_not_marked_failed(self, monkeypatch):
        # A PR genuinely absent from an otherwise-good response (e.g. deleted)
        # is a legitimate "no data" — not a fetch failure that should preserve
        # stale prior data forever.
        monkeypatch.setattr(responses, "_CHUNK", 1)
        monkeypatch.setattr(responses.gh.subprocess, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=0,
                                                  stdout=json.dumps({"data": {"repository": {}}}),
                                                  stderr=""))
        nodes, failed = responses._fetch([9])
        assert nodes == {}
        assert failed == []


@pytest.fixture
def stores(monkeypatch, tmp_path) -> Iterator[tuple[Store, IssueStore]]:
    """Throwaway PR and issue stores behind the app's snapshots."""
    store = Store(tmp_path)          # Store takes a root *directory*, not a db path
    monkeypatch.setattr(data, "_store", store)
    monkeypatch.setattr(responses.activity, "operator", lambda: {"name": "Ada"})
    issue_data.set_store_root(tmp_path)
    yield store, issue_data.store()
    issue_data.set_store_root(None)


def _seed_pr(store: Store, n: int, sig: dict | None = None) -> None:
    rec: dict = {"pr": n, "meta": {"title": "t", "state": "closed", "head_sha": "h"}}
    if sig is not None:
        rec["responses"] = {"pr": n, **sig}
    store.save_pr(rec)


def _seed_issue(store: IssueStore, n: int, sig: dict | None = None) -> None:
    rec: dict = {"issue": n, "meta": {"title": "t", "state": "closed", "updated_at": ACT}}
    if sig is not None:
        rec["responses"] = {"issue": n, **sig}
    store.save_issue(rec)


class TestScanFailureResilience:
    """scan() must not erase a PR's previously-detected response just because
    this sweep's gh call for its chunk failed (#537)."""

    PRIOR = {"reopened": False, "new_commits": False, "replied": True,
             "resubmitted": False, "resubmitted_pr": None,
             "last_response_at": AFTER, "snippet": "why?", "by": "alice",
             "our_action": {"kind": "close", "at": ACT}, "detected_at": ACT}

    def test_scan_preserves_prior_entry_when_chunk_fetch_fails(self, monkeypatch, stores):
        _seed_pr(stores[0], 99, self.PRIOR)
        monkeypatch.setattr(responses, "_acted_baseline",
                            lambda: {"pr": {99: {"at": ACT, "kind": "close"}}, "issue": {}})
        monkeypatch.setattr(responses, "_fetch", lambda prs, kind: ({}, [99] if kind == "pr" else []))

        out = responses.scan()

        assert out == {"checked": 1, "with_response": 1, "prs": [99], "issues": [],
                       "failed": [99], "failed_issues": []}
        assert responses.recorded()["pr"][99]["replied"] is True  # carried forward untouched

    def test_scan_drops_entry_when_chunk_succeeds_with_no_signal(self, monkeypatch, stores):
        # Regression guard: only a *failed* chunk preserves stale data — a
        # successful re-check that finds nothing still clears the entry.
        _seed_pr(stores[0], 99, self.PRIOR)
        monkeypatch.setattr(responses, "_acted_baseline",
                            lambda: {"pr": {99: {"at": ACT, "kind": "close"}}, "issue": {}})
        node = {"number": 99, "state": "closed", "author": {"login": "alice"},
               "comments": {"nodes": []}, "timelineItems": {"nodes": []}, "commits": {"nodes": []}}
        monkeypatch.setattr(responses, "_fetch", lambda prs, kind: ({99: node} if kind == "pr" else {}, []))

        out = responses.scan()

        assert out["failed"] == []
        assert stores[0].load_pr(99).section("responses") is None

    def test_a_full_scan_clears_an_item_we_no_longer_measure(self, monkeypatch, stores):
        _seed_pr(stores[0], 99, self.PRIOR)
        monkeypatch.setattr(responses, "_acted_baseline", lambda: {"pr": {}, "issue": {}})
        monkeypatch.setattr(responses, "_fetch", lambda prs, kind: ({}, []))
        responses.scan()
        assert stores[0].load_pr(99).section("responses") is None

    def test_a_rescan_keeps_the_ack(self, monkeypatch, stores):
        _seed_pr(stores[0], 99, self.PRIOR)
        responses.ack(99, at=LATER)
        monkeypatch.setattr(responses, "_acted_baseline",
                            lambda: {"pr": {99: {"at": ACT, "kind": "close"}}, "issue": {}})
        node = {"number": 99, "state": "closed", "author": {"login": "alice"},
                "comments": {"nodes": [_comment("bob", LATER)]},
                "timelineItems": {"nodes": []}, "commits": {"nodes": []}}
        monkeypatch.setattr(responses, "_fetch", lambda prs, kind: ({99: node} if kind == "pr" else {}, []))
        responses.scan()
        rec = stores[0].load_pr(99)
        assert rec.section("responses")["by"] == "bob"
        assert rec.section("response_ack") == {"at": LATER, "by": "Ada"}


class TestAck:
    """A marker that an operator has seen a PR's response, shared across
    operators, until a newer response supersedes it (#537)."""

    def test_unacked_signal_has_no_ack(self, stores):
        _seed_pr(stores[0], 42, {"replied": True, "last_response_at": AFTER})
        sig = responses.signal(data.prs()[42])
        assert sig["replied"] is True
        assert sig["ack"] is None

    def test_ack_attaches_who_and_when(self, stores):
        _seed_pr(stores[0], 42, {"replied": True, "last_response_at": AFTER})
        responses.ack(42, at=LATER)
        sig = responses.signal(data.prs()[42])
        assert sig["replied"] is True          # the signal still renders
        assert sig["ack"] == {"at": LATER, "by": "Ada"}

    def test_a_newer_response_supersedes_the_ack(self, stores):
        _seed_pr(stores[0], 42, {"replied": True, "last_response_at": LATER})
        responses.ack(42, at=AFTER)
        assert responses.signal(data.prs()[42])["ack"] is None

    def test_ack_is_scoped_to_its_own_pr(self, stores):
        for n in (1, 2):
            _seed_pr(stores[0], n, {"replied": True, "last_response_at": AFTER})
        responses.ack(1, at=LATER)
        assert responses.signal(data.prs()[1])["ack"] is not None
        assert responses.signal(data.prs()[2])["ack"] is None

    def test_ack_defaults_to_now_and_records_the_operator(self, stores):
        _seed_pr(stores[0], 42, {"replied": True, "last_response_at": AFTER})
        rec = responses.ack(42)
        assert rec["by"] == "Ada"
        assert responses.signal(data.prs()[42])["ack"] == rec  # "now" is well after AFTER

    def test_an_ack_takes_the_pr_out_of_the_responses_queue(self, stores):
        stores[0].save_pr(_closed_pr({"replied": True, "last_response_at": AFTER}).raw)
        assert service.query_prs({"responses": "replied"})["total"] == 1
        responses.ack(42, at=LATER)
        assert service.query_prs({"responses": "replied"})["total"] == 0

    def test_ack_of_a_pr_not_in_the_store(self, stores):
        with pytest.raises(KeyError):
            responses.ack(42)


class TestIssues:
    """Issues we closed are swept beside the PRs, keyed by kind."""

    def test_baseline_keeps_issues_apart_and_only_our_closes(self):
        ev = lambda at, kind, **k: {"at": at, "kind": kind, "status": "executed",
                                    "dry_run": False, **k}
        events = [ev(LATER, "issue-reopen", issue=8),
                  ev(AFTER, "issue-close", issue=7),
                  ev(AFTER, "close", pr=7),
                  ev(ACT, "issue-close", issue=8)]
        base = responses._acted_baseline(events)
        assert base["pr"] == {7: {"at": AFTER, "kind": "close"}}
        assert base["issue"] == {7: {"at": AFTER, "kind": "issue-close"}}

    def test_issue_query_reads_issue_aliases_without_commits(self):
        q = responses._query([5, 6], "issue")
        assert "p0: issue(number: 5)" in q and "p1: issue(number: 6)" in q
        assert "pullRequest(number" not in q and "commits" not in q

    def test_reopened_issue_reads_as_reopened(self):
        sig = responses.classify(ACT, "issue-close", state="open", comments=[], reopens=[],
                                 commit_dates=[])
        assert sig is not None and sig["reopened"] is True

    def test_scan_records_an_issue_authors_mentions_without_touching_the_same_pr_number(
            self, monkeypatch, stores):
        _seed_pr(stores[0], 7)
        _seed_issue(stores[1], 7)
        monkeypatch.setattr(responses, "_acted_baseline", lambda: {
            "pr": {7: {"at": ACT, "kind": "close"}},
            "issue": {7: {"at": ACT, "kind": "issue-close"}}})
        issue = {"number": 7, "state": "CLOSED", "author": {"login": "alice"},
                 "comments": {"nodes": [_comment("alice", AFTER, "filed #12 instead")]},
                 "timelineItems": {"nodes": []}}
        asked: list[tuple[list[int], str]] = []

        def fetch(numbers, kind):
            asked.append((list(numbers), kind))
            return ({7: issue} if kind == "issue" else {}, [])
        monkeypatch.setattr(responses, "_fetch", fetch)

        out = responses.scan()
        assert asked == [([7], "pr"), ([7], "issue")]
        assert (out["prs"], out["issues"]) == ([], [7])
        sig = stores[1].load_issue(7).section("responses")
        assert sig["mentions"] == [12] and sig["issue"] == 7
        assert stores[0].load_pr(7).section("responses") is None

    def test_a_partial_scan_keeps_every_other_entry(self, monkeypatch, stores):
        for n in (1, 2):
            _seed_pr(stores[0], n, {"replied": True})
        _seed_issue(stores[1], 3, {"replied": True})
        monkeypatch.setattr(responses, "_acted_baseline", lambda: {
            "pr": {1: {"at": ACT, "kind": "close"}, 2: {"at": ACT, "kind": "close"}},
            "issue": {3: {"at": ACT, "kind": "issue-close"}}})
        monkeypatch.setattr(responses, "_fetch", lambda numbers, kind: ({}, []))
        responses.scan(prs=[1])
        reg = responses.recorded()
        assert sorted(reg["pr"]) == [2] and sorted(reg["issue"]) == [3]
