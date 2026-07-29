import json

from pipeline import ingest
from pipeline import reingest
from pipeline.store import Store
from pipeline.testsupport import set_section


def _upsert(store, n, sha, *, greptile=5, ci="passing", state="open"):
    ingest.upsert_pr(store, {"number": n, "title": f"pr {n}", "user": {"login": "a"},
                             "state": state, "head": {"sha": sha}, "base": {"ref": "master"},
                             "html_url": "u"},
                     ci_override=ci, greptile_override=greptile)
    store.load_pr(n).record_live_state(mergeable=True)  # the live sweep owns mergeable


def _no_network(monkeypatch):
    """Neutralize the deterministic-but-networked steps so a unit test never
    touches GitHub: refresh is a no-op that reports the head unchanged, the diff
    is 'already cached', and the threat rescan does nothing."""
    monkeypatch.setattr(reingest.ingest, "refresh_prs",
                        lambda s, nums, **k: [{"pr": int(nums[0]), "moved": False,
                                               "old_sha": "sha", "new_sha": "sha"}])
    monkeypatch.setattr(reingest.diff_cache, "fetch_diff", lambda pr, sha: True)
    monkeypatch.setattr(reingest, "_threat_rescan", lambda pr: None)


def test_not_in_store_returns_error_without_refreshing(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    # refresh must never run for an unknown PR (we return before touching GitHub).
    monkeypatch.setattr(reingest.ingest, "refresh_prs",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("refreshed")))
    assert reingest.run(store, 999) == 1


def test_noop_when_every_section_current(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _upsert(store, 100, "sha2")
    set_section(store, 100, "summary", {"one_liner": "x", "mechanism": "m", "subsystem": "ui",
                "identifiers": [], "paths": [], "schema_version": 2}, against_head_sha="sha2")
    set_section(store, 100, "cluster", {"ids": []}, against_head_sha="sha2")
    set_section(store, 100, "analysis", {"disposition": "merge", "rationale": "clean"},
                against_head_sha="sha2")
    _no_network(monkeypatch)
    # Any agent call would be a bug on the no-op path.
    monkeypatch.setattr(reingest.headless_agent, "run_agent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran agent")))

    assert reingest.run(store, 100) == 0
    assert store.load_pr(100).disposition == "merge"
    run = store.runs()[-1]
    assert run.phase == "reingest"
    assert run.raw["pr"] == 100
    assert run.raw["stats"] == {
        "status": "ok", "moved": False, "old_sha": "sha2", "new_sha": "sha2"}


def test_standalone_redispositioned_deterministically(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _upsert(store, 100, "sha2")                    # clean at the current head
    set_section(store, 100, "summary", {"one_liner": "x", "mechanism": "m", "subsystem": "ui",
                "identifiers": [], "paths": [], "schema_version": 2}, against_head_sha="sha2")
    set_section(store, 100, "cluster", {"ids": []}, against_head_sha="sha2")  # confirmed standalone
    set_section(store, 100, "analysis", {"disposition": "merge", "rationale": "old"},
                against_head_sha="sha1")           # stale — from a prior head
    _no_network(monkeypatch)
    # A standalone re-disposition is deterministic (disposition_orphans) — no agent.
    monkeypatch.setattr(reingest.headless_agent, "run_agent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran agent")))

    assert reingest.run(store, 100) == 0
    rec = store.load_pr(100)
    assert rec.disposition == "merge"
    assert rec.section("analysis")["against_head_sha"] == "sha2"   # pinned to current head


def test_standalone_redispositioned_after_head_moves_cluster_stamp_stale(tmp_path, monkeypatch):
    """#585: a resubmit moves a standalone PR's head, staling its `cluster` stamp
    (reingest never re-clusters) along with `analysis`. disposition_orphans
    requires a current `cluster` stamp, so without re-stamping it the PR would be
    treated as unconfirmed-standalone and left un-dispositioned forever."""
    store = Store(str(tmp_path))
    _upsert(store, 100, "sha2")                    # head moved to sha2
    set_section(store, 100, "summary", {"one_liner": "x", "mechanism": "m", "subsystem": "ui",
                "identifiers": [], "paths": [], "schema_version": 2}, against_head_sha="sha2")
    set_section(store, 100, "cluster", {"ids": []}, against_head_sha="sha1")  # stale: pre-resubmit head
    set_section(store, 100, "analysis", {"disposition": "merge", "rationale": "old"},
                against_head_sha="sha1")           # stale — from the pre-resubmit head
    _no_network(monkeypatch)
    monkeypatch.setattr(reingest.headless_agent, "run_agent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran agent")))

    assert reingest.run(store, 100) == 0
    rec = store.load_pr(100)
    assert rec.section("cluster")["ids"] == []
    assert rec.section("cluster")["against_head_sha"] == "sha2"    # re-stamped, still standalone
    assert rec.disposition == "merge"
    assert rec.section("analysis")["against_head_sha"] == "sha2"   # re-pinned to the current head


def test_clustered_pr_resummarized_and_reanalyzed(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _upsert(store, 100, "sha2")
    set_section(store, 100, "summary", {"one_liner": "x", "mechanism": "m",
                "subsystem": "ui", "identifiers": [], "paths": []}, against_head_sha="sha1")
    set_section(store, 100, "cluster", {"ids": [1]}, against_head_sha="sha1")
    set_section(store, 100, "analysis", {"disposition": "request-changes", "rationale": "old"},
                against_head_sha="sha1")
    store.save_cluster({"id": 1, "root_problem": "p", "prs": [100], "outcome": "awaiting-authors"})
    _no_network(monkeypatch)

    summary_item = {"pr": 100, "head_sha": "sha2", "one_liner": "y", "mechanism": "m2",
                    "subsystem": "ui", "identifiers": [], "paths": [], "primary_change": "y",
                    "secondary_changes": []}
    analysis = {"cluster_id": 1, "outcome": "merge-ready", "rationale": "now clean",
                "prs": [{"pr": 100, "head_sha": "sha2", "disposition": "merge", "rationale": "best"}]}

    def fake_agent(prompt, **k):
        if prompt.startswith("You are summarizing"):
            return "```json\n" + json.dumps({"items": [summary_item]}) + "\n```"
        return "```json\n" + json.dumps(analysis) + "\n```"

    monkeypatch.setattr(reingest.headless_agent, "run_agent", fake_agent)
    monkeypatch.setattr(reingest.reformat_rationales, "reformat_one",
                        lambda source: {"tier": "haiku", "summary": "TL;DR", "body": "**clean**"})

    assert reingest.run(store, 100) == 0
    rec = store.load_pr(100)
    assert rec.disposition == "merge"
    assert rec.section("summary")["against_head_sha"] == "sha2"
    assert rec.section("analysis")["against_head_sha"] == "sha2"
    assert store.load_cluster(1).outcome == "merge-ready"
    assert store.load_cluster(1).rationale_summary == "TL;DR"


def test_closed_pr_refreshes_meta_then_stops(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _upsert(store, 100, "sha2", state="closed")
    set_section(store, 100, "analysis", {"disposition": "merge", "rationale": "old"},
                against_head_sha="sha1")
    _no_network(monkeypatch)
    monkeypatch.setattr(reingest.headless_agent, "run_agent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran agent")))

    assert reingest.run(store, 100) == 0   # nothing to analyze on a closed PR


def test_invalid_cluster_analysis_aborts_without_writing(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _upsert(store, 100, "sha2")
    set_section(store, 100, "summary", {"one_liner": "x", "mechanism": "m", "subsystem": "ui",
                "identifiers": [], "paths": [], "schema_version": 2}, against_head_sha="sha2")
    set_section(store, 100, "cluster", {"ids": [1]}, against_head_sha="sha1")
    set_section(store, 100, "analysis", {"disposition": "request-changes", "rationale": "old"},
                against_head_sha="sha1")
    store.save_cluster({"id": 1, "root_problem": "p", "prs": [100], "outcome": "awaiting-authors"})
    _no_network(monkeypatch)
    # Agent omits the active member's disposition -> commit_analysis returns errors.
    bad = {"cluster_id": 1, "outcome": "close-out", "rationale": "x", "prs": []}
    monkeypatch.setattr(reingest.headless_agent, "run_agent",
                        lambda *a, **k: json.dumps(bad))

    assert reingest.run(store, 100) == 1
    assert store.load_cluster(1).outcome == "awaiting-authors"   # untouched
