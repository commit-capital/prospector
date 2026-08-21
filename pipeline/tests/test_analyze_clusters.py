"""Headless bulk per-cluster ANALYZE: cluster selection, concurrency, and
store commits, with the agent stubbed — no claude subprocess, no GitHub."""
import json
import threading

from pipeline import analyze_clusters
from pipeline import analyze_driver as ad
from pipeline.store import Store
from pipeline.testsupport import reviews_section

NOW = "2026-06-10T00:00:00+00:00"


def _pr(store, n, head="h1", author="a"):
    store.save_pr({"pr": n, "meta": {"title": f"t{n}", "author": author, "state": "open",
                                     "draft": False, "head_sha": head, "checked_at": NOW},
                   "signals": {"ci": "passing", "mergeable": True,
                               "checked_at": NOW, "against_head_sha": head},
                   "reviews": reviews_section(head, NOW),
                   "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": head},
                   "summary": {"one_liner": f"does {n}", "subsystem": "ui", "mechanism": "m",
                               "identifiers": [], "paths": [], "primary_change": f"primary {n}",
                               "secondary_changes": [], "checked_at": NOW, "against_head_sha": head}})


def _cluster(store, cid, prs):
    store.save_cluster({"id": cid, "root_problem": f"problem {cid}", "prs": [],
                        "outcome": None, "checked_at": NOW})
    store.edit_cluster(cid).set_members(prs)
    return store.load_cluster(cid)


def _seed(tmp_path, n_clusters: int) -> Store:
    st = Store(tmp_path)
    for cid in range(1, n_clusters + 1):
        _pr(st, cid)
        _cluster(st, cid, [cid])
    return st


def _fenced(cid: int, disposition: str = "merge") -> str:
    payload = {"cluster_id": cid, "outcome": "merge-ready", "rationale": "r",
              "prs": [{"pr": cid, "head_sha": "h1", "disposition": disposition, "rationale": "r"}]}
    return "```json\n" + json.dumps(payload) + "\n```"


def _bundle_cluster_id(prompt: str) -> int:
    path = next(tok for tok in prompt.split() if "analyze-cluster-" in tok)
    bundle = json.loads(open(path.rstrip(".,—")).read())
    return bundle["cluster"]["id"]


def test_run_cluster_agent_prompt_carries_bundle_path(monkeypatch):
    seen = {}

    def fake_run(prompt, **kw):
        seen["prompt"] = prompt
        return _fenced(7)

    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent", fake_run)
    payload = analyze_clusters.run_cluster_agent(7, {"cluster": {"id": 7}, "members": []})
    assert "__BUNDLE_PATH__" not in seen["prompt"]
    assert payload["cluster_id"] == 7


def test_main_batches_and_reports(tmp_path, monkeypatch, capsys):
    """--limit caps the run, clusters run concurrently, and a failing cluster
    doesn't stop the rest or the others' commits."""
    _seed(tmp_path, 5)
    calls = []

    def fake_run(prompt, **kw):
        calls.append(prompt)
        cid = _bundle_cluster_id(prompt)
        if cid == 1:                        # cluster 1 always fails
            raise RuntimeError("boom")
        return _fenced(cid)

    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent", fake_run)
    rc = analyze_clusters.main(["--limit", "4", "--store", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(calls) == 4                  # clusters 1-4, 5 beyond --limit
    assert "failed, continuing: boom" in out
    st2 = Store(tmp_path)
    assert st2.load_pr(1).disposition is None    # its agent call raised
    assert st2.load_pr(2).disposition == "merge"
    assert st2.load_pr(3).disposition == "merge"
    assert st2.load_pr(4).disposition == "merge"
    assert st2.load_pr(5).disposition is None    # beyond --limit
    assert [c.id for c in ad.pending(st2)] == [1, 5]


def test_main_records_run_with_attempted_count(tmp_path, monkeypatch):
    """The run-ledger entry carries a real `started` (not the same call as
    `finished`) plus `stats.attempted`, so a seconds-per-cluster rate can be
    computed from it (pipeline_status._seconds_per_unit)."""
    _seed(tmp_path, 3)
    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent",
                        lambda prompt, **kw: _fenced(_bundle_cluster_id(prompt)))
    rc = analyze_clusters.main(["--limit", "2", "--store", str(tmp_path)])
    assert rc == 0
    st = Store(tmp_path)
    runs = [r for r in st.runs() if r.phase == "analyze:commit"]
    assert len(runs) == 1
    assert runs[0].started is not None
    assert runs[0].raw["stats"]["attempted"] == 2


def test_main_runs_clusters_concurrently(tmp_path, monkeypatch):
    """With --concurrency > 1 the clusters' agents overlap in time rather than
    running strictly one after another."""
    _seed(tmp_path, 3)
    barrier = threading.Barrier(3, timeout=5)   # 3 clusters must all be in-flight at once

    def fake_run(prompt, **kw):
        barrier.wait()                          # deadlocks (TimeoutError) unless run in parallel
        return _fenced(_bundle_cluster_id(prompt))

    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent", fake_run)
    rc = analyze_clusters.main(
        ["--limit", "3", "--concurrency", "3", "--store", str(tmp_path)])
    assert rc == 0
    st = Store(tmp_path)
    assert all(st.load_pr(n).disposition == "merge" for n in range(1, 4))


def test_main_nothing_pending(tmp_path, monkeypatch, capsys):
    st = _seed(tmp_path, 1)
    errs = ad.commit_analysis(st, json.loads(_fenced(1)[len("```json\n"):-len("\n```")]))
    assert errs == []
    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no agent call")))
    rc = analyze_clusters.main(["--store", str(tmp_path)])
    assert rc == 0
    assert "nothing pending" in capsys.readouterr().out


def test_invalid_payload_is_not_committed(tmp_path, monkeypatch, capsys):
    st = _seed(tmp_path, 1)
    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent",
                        lambda prompt, **kw: "```json\n"
                        + json.dumps({"cluster_id": 1, "outcome": "not-a-real-outcome",
                                      "rationale": "r", "prs": []})
                        + "\n```")
    rc = analyze_clusters.main(["--store", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "validation failed" in out
    assert st.load_pr(1).disposition is None
