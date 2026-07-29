"""recluster: scoped stage A + B for one cluster (agents mocked)."""
import json
from pipeline import recluster as rc
from pipeline.freshness import is_current
from pipeline.store import Store

NOW = "2026-06-10T00:00:00+00:00"


def _seed(store):
    for n in (100, 200, 300):
        store.save_pr({"pr": n,
                       "meta": {"title": f"t{n}", "author": "a", "state": "open",
                                "draft": False, "head_sha": f"h{n}", "checked_at": NOW},
                       "cluster": {"ids": [1], "checked_at": NOW, "against_head_sha": f"h{n}"}})
    store.save_cluster({"id": 1, "root_problem": "badge over-count",
                        "prs": [100, 200, 300], "outcome": None, "checked_at": NOW})


def _mk(n, prim, sub="inbox"):
    return {"pr": n, "head_sha": f"h{n}", "one_liner": prim, "mechanism": "m",
            "subsystem": sub, "identifiers": [], "paths": [],
            "primary_change": prim, "secondary_changes": []}


def _fake_agent_factory(cluster_payload):
    def fake(prompt, **kw):
        if "summarizing pull-request diffs" in prompt:           # stage A
            payload = {"items": [_mk(100, "badge counts read issues too"),
                                 _mk(200, "badge counts read issues too"),
                                 _mk(300, "agent inbox excludes routine_execution issues")]}
        else:                                                    # stage B
            payload = cluster_payload
        return "```json\n" + json.dumps(payload) + "\n```"
    return fake


def test_recluster_ejects_odd_member(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _seed(store)
    monkeypatch.setattr(rc.diff_cache, "fetch_diff", lambda *a, **k: True)
    # Stage B keeps the two genuine badge PRs, drops #300 (different primary intent).
    monkeypatch.setattr(rc.headless_agent, "run_agent", _fake_agent_factory(
        {"clusters": [{"root_problem": "badge over-count", "prs": [100, 200],
                       "confidence": "high"}]}))

    assert rc.run(store, 1) == 0
    # cluster keeps its id, narrowed to the survivors; the odd member is ejected.
    assert sorted(store.load_cluster(1).prs) == [100, 200]
    assert store.load_pr(100).cluster_ids == [1]
    # #300 was considered by the pass and left out → standalone (no ids), not
    # "never clustered": a current cluster stamp with no membership.
    assert store.load_pr(300).cluster_ids == [] and is_current(store.load_pr(300), "cluster")
    # stage A wrote dominance-aware summaries for every member.
    assert store.load_pr(300).section("summary")["primary_change"].startswith("agent inbox excludes")


def test_recluster_unknown_cluster_errors(tmp_path):
    assert rc.run(Store(str(tmp_path)), 999) != 0


def test_recluster_too_small_is_noop(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    store.save_pr({"pr": 100, "meta": {"title": "t", "author": "a", "state": "open",
                   "draft": False, "head_sha": "h", "checked_at": NOW}})
    store.save_cluster({"id": 1, "root_problem": "p", "prs": [100], "checked_at": NOW})
    # No agent should be invoked for a <2-member cluster.
    def boom(*a, **k):
        raise AssertionError("agent should not run")
    monkeypatch.setattr(rc.headless_agent, "run_agent", boom)
    assert rc.run(store, 1) == 0
