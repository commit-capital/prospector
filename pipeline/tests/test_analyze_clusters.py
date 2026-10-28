"""Headless bulk per-cluster ANALYZE: cluster selection, concurrency, and
store commits, with the agent stubbed — no claude subprocess, no GitHub."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Iterator
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import analyze_clusters
from pipeline import analyze_driver as ad
from pipeline import headless_agent
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


def test_main_writes_the_analyzed_prs_summary_cards(tmp_path, monkeypatch):
    _seed(tmp_path, 3)
    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent",
                        lambda prompt, **kw: _fenced(_bundle_cluster_id(prompt)))
    card = {"problem": "It broke.", "fix": "It mends it.", "how": ["It mends it."]}
    monkeypatch.setattr(analyze_clusters.display_pass, "ask", lambda prompt: card)
    monkeypatch.setattr(analyze_clusters.display_pass, "_issue_lines", lambda pr: "(none)")
    assert analyze_clusters.main(["--limit", "2", "--store", str(tmp_path)]) == 0
    st = Store(tmp_path)
    assert [bool(analyze_clusters.display_pass.current(st.load_pr(n))) for n in (1, 2, 3)] \
        == [True, True, False]


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


def test_main_stops_at_the_first_usage_limit_hit(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, 3)
    reset = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
    calls: list[int] = []

    def fake_agent(cid, bundle):
        calls.append(cid)
        raise headless_agent.CapacityExhausted("usage limit reached", resets_at=reset)

    monkeypatch.setattr(analyze_clusters, "run_cluster_agent", fake_agent)
    rc = analyze_clusters.main(["--concurrency", "1", "--store", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert len(calls) == 1
    assert (f"AI usage limit reached — resets at {reset.astimezone():%H:%M} "
            "(2 batch(es) not started); stopping.") in out
    assert [r.phase for r in Store(tmp_path).runs()].count("analyze:commit") == 1


def test_main_nothing_pending(tmp_path, monkeypatch, capsys):
    st = _seed(tmp_path, 1)
    errs = ad.commit_analysis(st, json.loads(_fenced(1)[len("```json\n"):-len("\n```")]))
    assert errs == []
    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no agent call")))
    rc = analyze_clusters.main(["--store", str(tmp_path)])
    assert rc == 0
    assert "nothing pending" in capsys.readouterr().out


def test_resume_after_last_cluster_commit_finishes_post_steps(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = _seed(tmp_path, 1)
    assert ad.commit_analysis(st, json.loads(_fenced(1)[len("```json\n"):-len("\n```")])) == []
    _pr(st, 9)
    st.edit_pr(9).mark_standalone()
    salvage: list[str] = []
    monkeypatch.setattr(ad, "backfill_salvage_items",
                        lambda store, today: salvage.append(today) or 0)
    monkeypatch.setattr(analyze_clusters, "run_cluster_agent",
                        lambda *a: (_ for _ in ()).throw(AssertionError("already committed")))
    result = analyze_clusters.run(Store(tmp_path), limit=20, concurrency=1)
    assert result.attempted == 0
    assert Store(tmp_path).load_pr(9).section("analysis") is not None
    assert len(salvage) == 1


def test_interrupted_analysis_keeps_committed_clusters_and_resumes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = _seed(tmp_path, 3)
    calls: list[int] = []

    def agent(cid: int, bundle: dict) -> dict:
        calls.append(cid)
        return json.loads(_fenced(cid).removeprefix("```json\n").removesuffix("\n```"))

    completed = analyze_clusters.as_completed

    def interrupt(futures: Iterable[Future[dict]]) -> Iterator[Future[dict]]:
        yield next(completed(futures))
        raise KeyboardInterrupt

    monkeypatch.setattr(analyze_clusters, "run_cluster_agent", agent)
    monkeypatch.setattr(analyze_clusters, "as_completed", interrupt)
    with pytest.raises(KeyboardInterrupt):
        analyze_clusters.run(st, limit=20, concurrency=2)

    reopened = Store(tmp_path)
    pending = {c.id for c in ad.pending(reopened)}
    assert len(pending) == 2
    saved = ({1, 2, 3} - pending).pop()
    assert reopened.load_pr(saved).disposition == "merge"

    calls.clear()
    monkeypatch.setattr(analyze_clusters, "as_completed", completed)
    result = analyze_clusters.run(reopened, limit=20, concurrency=2)
    assert set(calls) == pending
    assert result.committed == 2
    assert ad.pending(Store(tmp_path)) == []


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


def test_a_commit_that_raises_is_charged_to_the_budget(tmp_path, monkeypatch):
    # The ledger row is the lane's daily budget; a commit or wrap-up that
    # raises must not let the same cluster be analyzed again uncharged.
    _seed(tmp_path, 1)
    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent",
                        lambda prompt, **kw: _fenced(_bundle_cluster_id(prompt)))

    def boom(store, payload):
        raise ValueError("bad pr number")
    monkeypatch.setattr(ad, "commit_analysis", boom)
    monkeypatch.setattr(analyze_clusters, "_post_steps",
                        lambda store: (_ for _ in ()).throw(RuntimeError("wrap-up broke")))
    with pytest.raises(RuntimeError):
        analyze_clusters.main(["--store", str(tmp_path)])
    [run] = [r for r in Store(tmp_path).runs() if r.phase == "analyze:commit"]
    assert run.raw["stats"]["attempted"] == 1 and run.raw["stats"]["failed"] == 1


def test_the_analyze_agent_reads_only_its_bundle_and_the_members_diffs(monkeypatch, tmp_path):
    import os
    real = tmp_path / "diffs"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    seen: dict = {}

    def fake_run(prompt, **kw):
        path = next(t for t in prompt.split() if "analyze-cluster-" in t).rstrip(".,—")
        seen.update(kw, bundle=json.load(open(path)))
        return _fenced(7)

    monkeypatch.setattr(analyze_clusters.headless_agent, "run_agent", fake_run)
    analyze_clusters.run_cluster_agent(7, {"cluster": {"id": 7}, "members": [
        {"pr": 1, "diff_path": str(link / "a.diff")}, {"pr": 2}]})
    resolved = os.path.realpath(real / "a.diff")
    assert seen["read_root"] == [seen["cwd"], resolved]
    assert seen["bundle"]["members"][0]["diff_path"] == resolved
    assert seen["allow_gh"] is True and list(seen["env_allow"]) == []
