"""Live freshness check (#25): compare upstream PR state against the store
snapshot and report divergences. The GraphQL fetch (live_states) is
monkeypatched; we only test the comparison logic."""
import logging

from prospector_app.backend import data
from prospector_app.backend import freshness_live
from pipeline.model import Pr

HEAD = "abc1234deadbeef"
NEWHEAD = "ffff999cafebabe"


def _store_pr(n=1, state="open", head=HEAD, ci="passing", mergeable=True, greptile=5):
    return Pr(None, {
        "pr": n,
        "meta": {"title": f"fix {n}", "author": "alice", "state": state,
                 "head_sha": head, "url": f"https://x/pull/{n}"},
        "signals": {"greptile": greptile, "ci": ci, "mergeable": mergeable,
                    "against_head_sha": head},
    })


def _patch(monkeypatch, store, live):
    monkeypatch.setattr(data, "prs", lambda: store)
    monkeypatch.setattr(data, "refresh", lambda: None)
    monkeypatch.setattr(freshness_live, "live_states", lambda prs: (live, set()))
    # check() persists observed drift to the shared store; neutralize that
    # side-effect here — these tests cover the divergence comparison only.
    monkeypatch.setattr(freshness_live, "persist_live", lambda *a, **k: [])


def test_no_divergence_when_state_matches(monkeypatch):
    _patch(monkeypatch, {1: _store_pr(1)},
           {1: {"state": "open", "merged": False, "head": HEAD,
                "mergeable": "MERGEABLE", "ci": "passing"}})
    item = freshness_live.check([1])["items"][0]
    assert item["reachable"] is True
    assert item["diverged"] == []


def test_merged_upstream_is_flagged(monkeypatch):
    _patch(monkeypatch, {1: _store_pr(1, state="open")},
           {1: {"state": "merged", "merged": True, "head": HEAD,
                "mergeable": "UNKNOWN", "ci": "passing"}})
    kinds = [d["kind"] for d in freshness_live.check([1])["items"][0]["diverged"]]
    assert "merged" in kinds


def test_closed_upstream_is_flagged(monkeypatch):
    _patch(monkeypatch, {1: _store_pr(1, state="open")},
           {1: {"state": "closed", "merged": False, "head": HEAD,
                "mergeable": "UNKNOWN", "ci": "passing"}})
    div = freshness_live.check([1])["items"][0]["diverged"]
    state = [d for d in div if d["kind"] == "state"][0]
    assert state["was"] == "open" and state["now"] == "closed"


def test_new_head_sha_is_flagged(monkeypatch):
    _patch(monkeypatch, {1: _store_pr(1, head=HEAD)},
           {1: {"state": "open", "merged": False, "head": NEWHEAD,
                "mergeable": "MERGEABLE", "ci": "passing"}})
    head = [d for d in freshness_live.check([1])["items"][0]["diverged"] if d["kind"] == "head"]
    assert head and head[0]["now"] == NEWHEAD[:7]


def test_new_conflicts_is_flagged(monkeypatch):
    _patch(monkeypatch, {1: _store_pr(1, mergeable=True)},
           {1: {"state": "open", "merged": False, "head": HEAD,
                "mergeable": "CONFLICTING", "ci": "passing"}})
    kinds = [d["kind"] for d in freshness_live.check([1])["items"][0]["diverged"]]
    assert "conflicts" in kinds


def test_ci_regression_is_flagged(monkeypatch):
    _patch(monkeypatch, {1: _store_pr(1, ci="passing")},
           {1: {"state": "open", "merged": False, "head": HEAD,
                "mergeable": "MERGEABLE", "ci": "failing"}})
    ci = [d for d in freshness_live.check([1])["items"][0]["diverged"] if d["kind"] == "ci"]
    assert ci and ci[0]["was"] == "passing" and ci[0]["now"] == "failing"


def test_ci_unknown_baseline_is_not_drift(monkeypatch):
    # store never resolved CI ("unknown") — live "passing" is not a regression.
    _patch(monkeypatch, {1: _store_pr(1, ci="unknown")},
           {1: {"state": "open", "merged": False, "head": HEAD,
                "mergeable": "MERGEABLE", "ci": "passing"}})
    kinds = [d["kind"] for d in freshness_live.check([1])["items"][0]["diverged"]]
    assert "ci" not in kinds


class TestGreptileDivergence:
    """The review provider's bar is a hard merge requirement, so a score against
    an earlier commit is its own staleness — and its remedy is a review
    re-trigger, not a re-analysis."""

    def _pr(self, reviewed):
        pr = _store_pr(1)
        pr.rec["signals"]["greptile_reviewed_sha"] = reviewed
        return pr

    def test_flagged_when_greptile_reviewed_an_earlier_commit(self, monkeypatch):
        _patch(monkeypatch, {1: self._pr(HEAD)},
               {1: {"state": "open", "merged": False, "head": NEWHEAD,
                    "mergeable": "MERGEABLE", "ci": "passing"}})
        div = freshness_live.check([1])["items"][0]["diverged"]
        grep = [d for d in div if d["kind"] == "greptile"]
        assert grep and grep[0]["was"] == HEAD[:7] and grep[0]["now"] == NEWHEAD[:7]

    def test_flagged_even_when_our_own_analysis_is_current(self, monkeypatch):
        # the head has not moved since we analyzed; only Greptile is behind.
        _patch(monkeypatch, {1: self._pr("older12")},
               {1: {"state": "open", "merged": False, "head": HEAD,
                    "mergeable": "MERGEABLE", "ci": "passing"}})
        kinds = [d["kind"] for d in freshness_live.check([1])["items"][0]["diverged"]]
        assert kinds == ["greptile"]

    def test_quiet_when_greptile_is_current(self, monkeypatch):
        _patch(monkeypatch, {1: self._pr(HEAD)},
               {1: {"state": "open", "merged": False, "head": HEAD,
                    "mergeable": "MERGEABLE", "ci": "passing"}})
        assert freshness_live.check([1])["items"][0]["diverged"] == []

    def test_quiet_when_greptile_never_reviewed(self, monkeypatch):
        _patch(monkeypatch, {1: _store_pr(1)},
               {1: {"state": "open", "merged": False, "head": NEWHEAD,
                    "mergeable": "MERGEABLE", "ci": "passing"}})
        kinds = [d["kind"] for d in freshness_live.check([1])["items"][0]["diverged"]]
        assert kinds == ["head"]


def test_unreachable_pr_marked_not_reachable(monkeypatch):
    _patch(monkeypatch, {1: _store_pr(1)}, {})  # live_states returns nothing
    item = freshness_live.check([1])["items"][0]
    assert item["reachable"] is False and item["diverged"] == []


def test_live_states_parses_diffstat_and_has_tests(monkeypatch):
    node = {"number": 1, "state": "OPEN", "merged": False, "headRefOid": HEAD,
            "mergeable": "MERGEABLE", "additions": 40, "deletions": 6, "changedFiles": 2,
            "files": {"nodes": [{"path": "src/app.ts"}, {"path": "src/app.test.ts"}]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}
    monkeypatch.setattr(freshness_live.live_prs, "gh_graphql",
                        lambda query: {"data": {"repository": {"p0": node}}})
    out, _ = freshness_live.live_states([1])
    assert out[1]["diffstat"] == {"additions": 40, "deletions": 6, "changed_files": 2}
    assert out[1]["has_tests"] is True
    assert out[1]["ci"] == "passing"   # existing parse still works


def test_live_states_has_tests_false_without_test_files(monkeypatch):
    node = {"number": 2, "state": "OPEN", "merged": False, "headRefOid": HEAD,
            "mergeable": "MERGEABLE", "additions": 3, "deletions": 1, "changedFiles": 1,
            "files": {"nodes": [{"path": "README.md"}]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}
    monkeypatch.setattr(freshness_live.live_prs, "gh_graphql",
                        lambda query: {"data": {"repository": {"p0": node}}})
    assert freshness_live.live_states([2])[0][2]["has_tests"] is False


def test_live_states_omits_incomplete_diffstat(monkeypatch):
    node = {"number": 2, "state": "OPEN", "merged": False, "headRefOid": HEAD,
            "mergeable": "MERGEABLE", "additions": None, "deletions": 1,
            "changedFiles": 1, "files": {"nodes": []}, "commits": {"nodes": []}}
    monkeypatch.setattr(freshness_live.live_prs, "gh_graphql",
                        lambda query: {"data": {"repository": {"p0": node}}})
    assert freshness_live.live_states([2]) == ({}, set())


def test_live_states_skips_failed_chunk(monkeypatch, caplog):
    """A failed gh request drops its chunk and logs the missing coverage."""
    monkeypatch.setattr(freshness_live.live_prs, "gh_graphql", lambda query: None)
    with caplog.at_level(logging.WARNING, logger="pipeline.live_prs"):
        out = freshness_live.live_states([1, 2, 3])
    assert out == ({}, set())
    assert any("live PR fetch failed" in r.message for r in caplog.records)


def test_live_states_keeps_resolved_aliases_when_one_errors(monkeypatch, caplog):
    """One dead alias in a batch (a PR scrubbed from GitHub) nulls its own node
    and rides an `errors` array; every other alias in the batch still lands."""
    node = {"number": 1, "state": "OPEN", "merged": False, "headRefOid": HEAD,
            "mergeable": "MERGEABLE", "additions": 3, "deletions": 1, "changedFiles": 1,
            "files": {"nodes": [{"path": "src/app.ts"}]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}
    envelope = {"data": {"repository": {"p0": node, "p1": None}},
                "errors": [{"type": "NOT_FOUND", "path": ["repository", "p1"],
                            "message": "Could not resolve to a PullRequest with the number of 10241."}]}
    monkeypatch.setattr(freshness_live.live_prs, "gh_graphql", lambda query: envelope)
    with caplog.at_level(logging.WARNING, logger="pipeline.live_prs"):
        out, not_found = freshness_live.live_states([1, 10241])
    assert 1 in out and out[1]["state"] == "open"
    assert 10241 not in out
    assert not_found == {10241}
    assert any("GraphQL error" in r.message for r in caplog.records)


def test_live_states_not_found_requires_the_not_found_type(monkeypatch):
    """Only an explicit NOT_FOUND error retires its alias — a transient error
    (rate limit, server hiccup) on the same shape leaves the number eligible
    for retry."""
    envelope = {"data": {"repository": {"p0": None, "p1": None}},
                "errors": [{"type": "NOT_FOUND", "path": ["repository", "p0"],
                            "message": "Could not resolve to a PullRequest with the number of 4818."},
                           {"type": "RATE_LIMITED", "path": ["repository", "p1"],
                            "message": "API rate limit exceeded"}]}
    monkeypatch.setattr(freshness_live.live_prs, "gh_graphql", lambda query: envelope)
    facts, not_found = freshness_live.live_states([4818, 5000])
    assert facts == {}
    assert not_found == {4818}


def test_live_states_not_found_ignores_a_repository_level_error(monkeypatch):
    """A NOT_FOUND on the repository itself (path ["repository"]) names no PR —
    nothing is marked, the whole batch just stays missing."""
    envelope = {"data": {"repository": None},
                "errors": [{"type": "NOT_FOUND", "path": ["repository"],
                            "message": "Could not resolve to a Repository."}]}
    monkeypatch.setattr(freshness_live.live_prs, "gh_graphql", lambda query: envelope)
    facts, not_found = freshness_live.live_states([1, 2])
    assert facts == {}
    assert not_found == set()
