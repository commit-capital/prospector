"""Live freshness check (#25): compare upstream PR state against the store
snapshot and report divergences. The GraphQL fetch (live_states) is
monkeypatched; we only test the comparison logic."""
import json
import logging
import subprocess
import types

from app.backend import data
from app.backend import freshness_live
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
    monkeypatch.setattr(freshness_live, "live_states", lambda prs: live)
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


def test_unreachable_pr_marked_not_reachable(monkeypatch):
    _patch(monkeypatch, {1: _store_pr(1)}, {})  # live_states returns nothing
    item = freshness_live.check([1])["items"][0]
    assert item["reachable"] is False and item["diverged"] == []


def _fake_run(payload):
    return lambda argv, *, timeout=60, **kw: types.SimpleNamespace(
        returncode=0, stdout=json.dumps(payload))


def test_live_states_parses_diffstat_and_has_tests(monkeypatch):
    node = {"number": 1, "state": "OPEN", "merged": False, "headRefOid": HEAD,
            "mergeable": "MERGEABLE", "additions": 40, "deletions": 6, "changedFiles": 2,
            "files": {"nodes": [{"path": "src/app.ts"}, {"path": "src/app.test.ts"}]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}
    monkeypatch.setattr(freshness_live, "run",
                        _fake_run({"data": {"repository": {"p0": node}}}))
    out = freshness_live.live_states([1])
    assert out[1]["diffstat"] == {"additions": 40, "deletions": 6, "changed_files": 2}
    assert out[1]["has_tests"] is True
    assert out[1]["ci"] == "passing"   # existing parse still works


def test_live_states_has_tests_false_without_test_files(monkeypatch):
    node = {"number": 2, "state": "OPEN", "merged": False, "headRefOid": HEAD,
            "mergeable": "MERGEABLE", "additions": 3, "deletions": 1, "changedFiles": 1,
            "files": {"nodes": [{"path": "README.md"}]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}
    monkeypatch.setattr(freshness_live, "run",
                        _fake_run({"data": {"repository": {"p0": node}}}))
    assert freshness_live.live_states([2])[2]["has_tests"] is False


def test_live_states_skips_chunk_on_gh_timeout(monkeypatch, caplog):
    """A wedged gh times out per-chunk: live_states drops that chunk (no raise,
    stays bounded) and logs a warning so the lag is visible (#322)."""
    def boom(argv, *, timeout=60, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
    monkeypatch.setattr(freshness_live, "run", boom)
    with caplog.at_level(logging.WARNING, logger="freshness_live"):
        out = freshness_live.live_states([1, 2, 3])
    assert out == {}
    assert any("timed out" in r.message for r in caplog.records)
