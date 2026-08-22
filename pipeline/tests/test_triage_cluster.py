import json
from pipeline import ingest
from pipeline import triage_cluster as tc
from pipeline.store import Store
from pipeline.testsupport import set_section
from pipeline.testsupport import greptile_entry


def _seed_cluster(store: Store, sha="sha1"):
    ingest.upsert_pr(store, {"number": 100, "title": "winner", "user": {"login": "a"},
                             "state": "open", "head": {"sha": sha}, "base": {"ref": "master"},
                             "html_url": "u"},
                     ci_override="passing",
                     reviews_override={"greptile": greptile_entry(5, sha)})
    store.load_pr(100).record_live_state(mergeable=True)  # the live sweep owns mergeable
    set_section(store, 100, "summary",
                {"one_liner": "x", "mechanism": "m", "subsystem": "ui",
                 "identifiers": [], "paths": []}, against_head_sha=sha)
    set_section(store, 100, "cluster", {"ids": [1]}, against_head_sha=sha)
    store.save_cluster({"id": 1, "root_problem": "p", "prs": [100], "outcome": "close-out"})


def test_triage_forces_reanalysis_even_when_fresh(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _seed_cluster(store)

    # No new commits.
    monkeypatch.setattr(tc.ingest, "refresh_prs",
                        lambda s, nums, **k: [{"pr": 100, "moved": False,
                                               "old_sha": "sha1", "new_sha": "sha1"}])
    # Analyst returns a NEW disposition (merge) — proves it re-ran despite freshness.
    analysis = {"cluster_id": 1, "outcome": "merge-ready", "rationale": "winner is clean",
                "prs": [{"pr": 100, "head_sha": "sha1", "disposition": "merge",
                         "rationale": "best"}]}
    monkeypatch.setattr(tc.headless_agent, "run_agent",
                        lambda *a, **k: "```json\n" + json.dumps(analysis) + "\n```")
    monkeypatch.setattr(tc.reformat_rationales, "reformat_one",
                        lambda source: {"tier": "haiku", "summary": "TL;DR",
                                        "body": "**winner** is clean"})

    tc.run(store, 1)

    rec = store.load_pr(100)
    assert rec.section("analysis")["disposition"] == "merge"
    cluster = store.load_cluster(1)
    assert cluster.outcome == "merge-ready"
    assert cluster.rationale == "**winner** is clean"
    assert cluster.rationale_summary == "TL;DR"


def test_triage_keeps_committed_analysis_when_formatting_fails(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _seed_cluster(store)
    monkeypatch.setattr(tc.ingest, "refresh_prs",
                        lambda s, nums, **k: [{"pr": 100, "moved": False,
                                               "old_sha": "sha1", "new_sha": "sha1"}])
    analysis = {"cluster_id": 1, "outcome": "merge-ready", "rationale": "winner is clean",
                "prs": [{"pr": 100, "head_sha": "sha1", "disposition": "merge",
                         "rationale": "best"}]}
    monkeypatch.setattr(tc.headless_agent, "run_agent",
                        lambda *a, **k: json.dumps(analysis))

    def formatting_failed(source):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(tc.reformat_rationales, "reformat_one", formatting_failed)

    assert tc.run(store, 1) == 0
    cluster = store.load_cluster(1)
    assert cluster.outcome == "merge-ready"
    assert cluster.rationale == "winner is clean"
    assert cluster.rationale_summary is None


def test_triage_aborts_on_invalid_analysis(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _seed_cluster(store)
    monkeypatch.setattr(tc.ingest, "refresh_prs",
                        lambda s, nums, **k: [{"pr": 100, "moved": False,
                                               "old_sha": "sha1", "new_sha": "sha1"}])
    # Missing the active member's disposition -> commit_analysis returns errors.
    bad = {"cluster_id": 1, "outcome": "close-out", "rationale": "x", "prs": []}
    monkeypatch.setattr(tc.headless_agent, "run_agent",
                        lambda *a, **k: json.dumps(bad))

    tc_code = tc.run(store, 1)
    assert tc_code != 0
    # Original outcome untouched (no partial write).
    assert store.load_cluster(1).outcome == "close-out"


def test_triage_unknown_cluster_returns_error(tmp_path):
    store = Store(str(tmp_path))
    assert tc.run(store, 999) != 0


def test_triage_agent_failure_returns_error_cleanly(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _seed_cluster(store)
    monkeypatch.setattr(tc.ingest, "refresh_prs",
                        lambda s, nums, **k: [{"pr": 100, "moved": False,
                                               "old_sha": "sha1", "new_sha": "sha1"}])
    def boom(*a, **k):
        raise RuntimeError("claude exited 1")
    monkeypatch.setattr(tc.headless_agent, "run_agent", boom)
    assert tc.run(store, 1) != 0
    assert store.load_cluster(1).outcome == "close-out"  # untouched
