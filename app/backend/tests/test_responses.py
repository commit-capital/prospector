"""Community response classification + the PR-queue widening that surfaces
pushback on PRs we already closed. classify() is pure; list_prs is exercised
with the registry + store monkeypatched."""
import json
import logging
import subprocess
import types

from pipeline import model
from pipeline.store import Store
from app.backend import responses
from app.backend import service
from app.backend import data

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


SAME_REPO = f"{responses.REPO_OWNER}/{responses.REPO_NAME}"


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
                                 comments=[_comment(responses.BOT_LOGIN, AFTER, typename="Bot"),
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
                                 comments=[], reopens=[_reopen(responses.BOT_LOGIN, AFTER)],
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


class TestQueueWidening:
    def test_responses_filter_surfaces_a_closed_pr(self, monkeypatch):
        # A PR we closed, with a reply — normally excluded (closed), but the
        # responses filter widens to any state.
        rec = model.Pr(None, {
            "pr": 42, "meta": {"title": "t", "author": "a", "state": "closed",
                               "draft": False, "head_sha": "h", "url": "x",
                               "created_at": ACT, "updated_at": AFTER, "checked_at": ACT},
            "signals": {"checked_at": ACT, "against_head_sha": "h"},
            "drift": {"state": "applicable", "checked_at": ACT, "against_head_sha": "h"}})
        monkeypatch.setattr(data, "prs", lambda: {42: rec})
        monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
        reg = {42: {"pr": 42, "reopened": False, "new_commits": False, "replied": True,
                    "last_response_at": AFTER, "snippet": "why?", "by": "a"}}
        monkeypatch.setattr(responses, "load", lambda: reg)
        monkeypatch.setattr(responses, "for_pr", lambda n: reg.get(int(n)))

        # without the filter the closed PR is absent
        assert service.query_prs({})["total"] == 0
        # with it, the closed PR surfaces, carrying its responses payload
        out = service.query_prs({"responses": "replied"})
        assert out["total"] == 1
        assert out["items"][0]["number"] == 42
        assert out["items"][0]["responses"]["replied"] is True

    def test_responses_filter_excludes_non_matching_signal(self, monkeypatch):
        rec = model.Pr(None, {
            "pr": 42, "meta": {"title": "t", "author": "a", "state": "closed",
                               "draft": False, "head_sha": "h", "url": "x",
                               "created_at": ACT, "updated_at": AFTER, "checked_at": ACT},
            "signals": {"checked_at": ACT, "against_head_sha": "h"},
            "drift": {"state": "applicable", "checked_at": ACT, "against_head_sha": "h"}})
        monkeypatch.setattr(data, "prs", lambda: {42: rec})
        monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
        reg = {42: {"pr": 42, "reopened": False, "new_commits": False, "replied": True,
                    "resubmitted": False, "resubmitted_pr": None,
                    "last_response_at": AFTER, "snippet": "why?", "by": "a"}}
        monkeypatch.setattr(responses, "load", lambda: reg)
        monkeypatch.setattr(responses, "for_pr", lambda n: reg.get(int(n)))
        # filtering for reopened (which this PR lacks) yields nothing
        assert service.query_prs({"responses": "reopened"})["total"] == 0

    def test_resubmitted_filter_surfaces_closed_pr(self, monkeypatch):
        rec = model.Pr(None, {
            "pr": 42, "meta": {"title": "t", "author": "a", "state": "closed",
                               "draft": False, "head_sha": "h", "url": "x",
                               "created_at": ACT, "updated_at": AFTER, "checked_at": ACT},
            "signals": {"checked_at": ACT, "against_head_sha": "h"},
            "drift": {"state": "applicable", "checked_at": ACT, "against_head_sha": "h"}})
        monkeypatch.setattr(data, "prs", lambda: {42: rec})
        monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
        reg = {42: {"pr": 42, "reopened": False, "new_commits": False, "replied": False,
                    "resubmitted": True, "resubmitted_pr": 99,
                    "last_response_at": AFTER, "snippet": None, "by": None}}
        monkeypatch.setattr(responses, "load", lambda: reg)
        monkeypatch.setattr(responses, "for_pr", lambda n: reg.get(int(n)))
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

        monkeypatch.setattr(responses, "run", fake_run)
        with caplog.at_level(logging.WARNING, logger="app.backend.responses"):
            nodes, failed = responses._fetch([10, 11, 12])
        assert failed == [11]
        assert set(nodes) == {10, 12}
        assert any("timed out" in r.message for r in caplog.records)

    def test_nonzero_returncode_with_no_body_marks_chunk_failed_and_logs(self, monkeypatch, caplog):
        monkeypatch.setattr(responses, "_CHUNK", 1)
        monkeypatch.setattr(responses, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=1, stdout="", stderr="rate limited"))
        with caplog.at_level(logging.WARNING, logger="app.backend.responses"):
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
        monkeypatch.setattr(responses, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=1, stdout=json.dumps(body),
                                                  stderr="gh: Could not resolve to a PullRequest "
                                                         "with the number of 6418."))
        with caplog.at_level(logging.WARNING, logger="app.backend.responses"):
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
        monkeypatch.setattr(responses, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=1, stdout=json.dumps(body), stderr=""))
        nodes, failed = responses._fetch([400, 401])
        assert set(nodes) == {401}
        assert failed == [400]

    def test_unparseable_response_marks_chunk_failed(self, monkeypatch):
        monkeypatch.setattr(responses, "_CHUNK", 1)
        monkeypatch.setattr(responses, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=0, stdout="not json", stderr=""))
        nodes, failed = responses._fetch([7])
        assert nodes == {}
        assert failed == [7]

    def test_missing_node_in_a_successful_chunk_is_not_marked_failed(self, monkeypatch):
        # A PR genuinely absent from an otherwise-good response (e.g. deleted)
        # is a legitimate "no data" — not a fetch failure that should preserve
        # stale prior data forever.
        monkeypatch.setattr(responses, "_CHUNK", 1)
        monkeypatch.setattr(responses, "run", lambda argv, *, timeout=90, **kw:
                            types.SimpleNamespace(returncode=0,
                                                  stdout=json.dumps({"data": {"repository": {}}}),
                                                  stderr=""))
        nodes, failed = responses._fetch([9])
        assert nodes == {}
        assert failed == []


class TestScanFailureResilience:
    """scan() must not erase a PR's previously-detected response just because
    this sweep's gh call for its chunk failed (#537)."""

    def _prior_entry(self, n=99):
        return {n: {"pr": n, "reopened": False, "new_commits": False, "replied": True,
                    "resubmitted": False, "resubmitted_pr": None,
                    "last_response_at": AFTER, "snippet": "why?", "by": "alice",
                    "our_action": {"kind": "close", "at": ACT}, "detected_at": ACT}}

    def test_scan_preserves_prior_entry_when_chunk_fetch_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(responses, "REG", tmp_path / "responses.json")
        monkeypatch.setattr(responses, "_cache", None)
        responses._save(self._prior_entry())
        monkeypatch.setattr(responses, "_acted_baseline", lambda: {99: {"at": ACT, "kind": "close"}})
        monkeypatch.setattr(responses, "_fetch", lambda prs: ({}, [99]))

        out = responses.scan()

        assert out == {"checked": 1, "with_response": 1, "prs": [99], "failed": [99]}
        assert responses.load()[99]["replied"] is True  # carried forward untouched

    def test_scan_drops_entry_when_chunk_succeeds_with_no_signal(self, monkeypatch, tmp_path):
        # Regression guard: only a *failed* chunk preserves stale data — a
        # successful re-check that finds nothing still clears the entry.
        monkeypatch.setattr(responses, "REG", tmp_path / "responses.json")
        monkeypatch.setattr(responses, "_cache", None)
        responses._save(self._prior_entry())
        monkeypatch.setattr(responses, "_acted_baseline", lambda: {99: {"at": ACT, "kind": "close"}})
        node = {"number": 99, "state": "closed", "author": {"login": "alice"},
               "comments": {"nodes": []}, "timelineItems": {"nodes": []}, "commits": {"nodes": []}}
        monkeypatch.setattr(responses, "_fetch", lambda prs: ({99: node}, []))

        out = responses.scan()

        assert out["failed"] == []
        assert 99 not in responses.load()


class TestAck:
    """A marker that an operator has seen a PR's response, shared across
    operators, until a newer response supersedes it (#537)."""

    def _seed(self, monkeypatch, tmp_path, reg):
        """Local response registry + an isolated in-memory ack store."""
        monkeypatch.setattr(responses, "REG", tmp_path / "responses.json")
        monkeypatch.setattr(responses, "_cache", None)
        monkeypatch.setattr(responses, "_acks_cache", None)
        monkeypatch.setattr(responses.activity, "operator", lambda: {"name": "Ada"})
        store = Store(tmp_path)          # Store takes a root *directory*, not a db path
        monkeypatch.setattr(responses.data, "store", lambda: store)
        responses._save(reg)
        return store

    def test_unacked_signal_has_no_ack(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {42: {"pr": 42, "replied": True, "last_response_at": AFTER}})
        sig = responses.for_pr(42)
        assert sig["replied"] is True
        assert sig["ack"] is None

    def test_ack_attaches_who_and_when(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {42: {"pr": 42, "replied": True, "last_response_at": AFTER}})
        responses.ack(42, at=LATER)
        sig = responses.for_pr(42)
        assert sig["replied"] is True          # the signal still renders
        assert sig["ack"] == {"at": LATER, "by": "Ada"}

    def test_a_newer_response_supersedes_the_ack(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {42: {"pr": 42, "replied": True, "last_response_at": LATER}})
        responses.ack(42, at=AFTER)
        assert responses.for_pr(42)["ack"] is None

    def test_ack_is_scoped_to_its_own_pr(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {
            1: {"pr": 1, "replied": True, "last_response_at": AFTER},
            2: {"pr": 2, "replied": True, "last_response_at": AFTER}})
        responses.ack(1, at=LATER)
        assert responses.for_pr(1)["ack"] is not None
        assert responses.for_pr(2)["ack"] is None

    def test_ack_defaults_to_now_and_records_the_operator(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {42: {"pr": 42, "replied": True, "last_response_at": AFTER}})
        rec = responses.ack(42)
        assert responses.load_acks()[42] == rec
        assert rec["by"] == "Ada"
        assert responses.for_pr(42)["ack"] == rec  # "now" is well after AFTER (2026-06-11)

    def test_another_operators_ack_is_visible(self, monkeypatch, tmp_path):
        """The point of storing acks centrally: B sees what A cleared."""
        store = self._seed(monkeypatch, tmp_path, {42: {"pr": 42, "replied": True, "last_response_at": AFTER}})
        store.save_response_acks({"acks": {"42": {"at": LATER, "by": "Taylor"}}})
        monkeypatch.setattr(responses, "_acks_cache", None)  # B's app, cold cache
        assert responses.for_pr(42)["ack"] == {"at": LATER, "by": "Taylor"}

    def test_store_read_failure_shows_signals_unacked(self, monkeypatch, tmp_path):
        """Fails soft: a wobbly store must not 500 the explorer."""
        self._seed(monkeypatch, tmp_path, {42: {"pr": 42, "replied": True, "last_response_at": AFTER}})

        def boom():
            raise RuntimeError("store down")

        monkeypatch.setattr(responses.data, "store", boom)
        assert responses.for_pr(42)["ack"] is None

    def test_store_read_failure_is_cached(self, monkeypatch, tmp_path):
        """The failure itself is cached, not just successes — service.list_prs
        calls for_pr once per PR in the snapshot (~3,000), so an unreachable
        store must cost one connect attempt per TTL window, not one per row."""
        self._seed(monkeypatch, tmp_path, {42: {"pr": 42, "replied": True, "last_response_at": AFTER}})
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("store down")

        monkeypatch.setattr(responses.data, "store", boom)
        responses.for_pr(42)
        responses.for_pr(42)
        assert calls["n"] == 1
