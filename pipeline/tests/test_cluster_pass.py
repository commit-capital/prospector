"""cluster_pass: one headless incremental CLUSTER + ANALYZE pass."""
from __future__ import annotations

import json

import pytest

from pipeline import analyze_clusters, capacity, cluster_driver, cluster_pass, diff_cache, headless_agent
from pipeline.freshness import SECTION_SCHEMA_VERSION
from pipeline.store import Store

NOW = "2026-06-10T00:00:00+00:00"


def _pr(store: Store, n: int, *, summarized: bool = False, subsystem: str = "ui") -> None:
    head = f"h{n}"
    rec: dict = {"pr": n, "meta": {"title": f"t{n}", "author": "a", "state": "open",
                                   "draft": False, "head_sha": head, "checked_at": NOW}}
    if summarized:
        rec["summary"] = {"one_liner": f"o{n}", "subsystem": subsystem, "mechanism": "m",
                          "identifiers": [], "paths": [], "primary_change": f"p{n}",
                          "secondary_changes": [], "checked_at": NOW,
                          "against_head_sha": head,
                          "schema_version": SECTION_SCHEMA_VERSION["summary"]}
    store.save_pr(rec)


@pytest.fixture
def store(tmp_path, monkeypatch) -> Store:
    diffs = tmp_path / "diffs"
    diffs.mkdir()
    monkeypatch.setattr(diff_cache, "DIFFS", diffs)

    def fetch(manifest, store=None, **_):
        for m in manifest:
            (diffs / f"{m.head_sha}.diff").write_text("diff --git a/x b/x\n+x\n")
        return len(manifest), 0
    monkeypatch.setattr(diff_cache, "fetch_diffs", fetch)
    return Store(tmp_path / "store")


def _summarizer(calls: list[list[int]]):
    def run(batch, on_event=None):
        calls.append([e["pr"] for e in batch])
        items = [{"pr": e["pr"], "head_sha": e["head_sha"], "one_liner": f"o{e['pr']}",
                  "subsystem": "ui", "primary_change": f"p{e['pr']}"} for e in batch]
        return "```json\n" + json.dumps({"items": items}) + "\n```"
    return run


def _ledger(store: Store, phase: str) -> list[dict]:
    return [r.raw for r in store.runs() if getattr(r, "phase", None) == phase]


class TestSummarize:
    def test_summarizes_up_to_the_limit_in_batches(self, store, monkeypatch):
        for n in range(1, 8):
            _pr(store, n)
        calls: list[list[int]] = []
        monkeypatch.setattr(cluster_driver, "run_summarize_agent", _summarizer(calls))
        monkeypatch.setattr(cluster_pass, "SUMMARY_BATCH", 2)
        stage = cluster_pass.summarize(store, 5, 1, cluster_pass._Outage(), "worker")
        assert stage.attempted == 5 and stage.started == 3 and stage.errored == 0
        assert sorted(n for c in calls for n in c) == [1, 2, 3, 4, 5]
        assert all(store.load_pr(n).section("summary") for n in range(1, 6))
        assert store.load_pr(6).section("summary") is None
        [row] = _ledger(store, "cluster:summaries")
        assert row["trigger"] == "worker" and row["stats"]["attempted"] == 5

    def test_a_zero_limit_summarizes_nothing(self, store, monkeypatch):
        _pr(store, 1)
        monkeypatch.setattr(cluster_driver, "run_summarize_agent",
                            lambda *a, **k: pytest.fail("no agent should run"))
        stage = cluster_pass.summarize(store, 0, 1, cluster_pass._Outage(), None)
        assert stage.attempted == 0


class TestRestrict:
    def test_holds_the_answer_to_the_unit(self):
        unit = {"subsystem": "ui", "existing_clusters": [{"id": 5}],
                "new_prs": [{"pr": 1}, {"pr": 2}, {"pr": 3}]}
        payload = {"joins": [{"pr": 1, "cluster_id": 5}, {"pr": 2, "cluster_id": 9},
                             {"pr": 99, "cluster_id": 5}],
                   "new_clusters": [{"root_problem": "r", "prs": [2, 3, 99]},
                                    {"root_problem": "solo", "prs": [3, 98]}],
                   "standalone": [3, 97, "x"]}
        assert cluster_pass.restrict(payload, unit) == {
            "joins": [{"pr": 1, "cluster_id": 5}],
            "new_clusters": [{"root_problem": "r", "prs": [2, 3]}],
            "standalone": [3]}


class TestAssign:
    def test_a_later_round_sees_the_clusters_an_earlier_one_made(self, store, monkeypatch):
        for n in range(1, 5):
            _pr(store, n, summarized=True)
        monkeypatch.setattr(cluster_pass, "ASSIGN_CHUNK", 2)
        seen: list[tuple[list[int], list[int]]] = []

        def assign_agent(unit, on_event=None):
            prs = [p["pr"] for p in unit["new_prs"]]
            existing = [c["id"] for c in unit["existing_clusters"]]
            seen.append((prs, existing))
            if not existing:
                answer = {"joins": [], "new_clusters": [{"root_problem": "r", "prs": prs}],
                          "standalone": []}
            else:
                answer = {"joins": [{"pr": p, "cluster_id": existing[0]} for p in prs],
                          "new_clusters": [], "standalone": []}
            return "```json\n" + json.dumps(answer) + "\n```"
        monkeypatch.setattr(cluster_driver, "run_assign_agent", assign_agent)
        stage = cluster_pass.assign(store, 10, 1, cluster_pass._Outage(), "worker")
        assert stage.attempted == 4
        assert seen[0] == ([1, 2], [])
        assert seen[1][0] == [3, 4] and len(seen[1][1]) == 1
        cid = seen[1][1][0]
        assert sorted(store.load_cluster(cid).prs) == [1, 2, 3, 4]
        [row] = _ledger(store, "cluster:assign")
        assert row["stats"]["joined"] == 2 and row["stats"]["created"] == 1

    def test_a_pr_the_agent_skipped_is_not_offered_again(self, store, monkeypatch):
        for n in (1, 2):
            _pr(store, n, summarized=True)
        calls: list[list[int]] = []

        def assign_agent(unit, on_event=None):
            calls.append([p["pr"] for p in unit["new_prs"]])
            return '```json\n{"joins": [], "new_clusters": [], "standalone": [1]}\n```'
        monkeypatch.setattr(cluster_driver, "run_assign_agent", assign_agent)
        cluster_pass.assign(store, 10, 1, cluster_pass._Outage(), None)
        assert calls == [[1, 2]]
        assert store.load_pr(2).section("cluster") is None

    def test_the_limit_bounds_the_prs_offered(self, store, monkeypatch):
        for n in range(1, 6):
            _pr(store, n, summarized=True)
        calls: list[list[int]] = []

        def assign_agent(unit, on_event=None):
            calls.append([p["pr"] for p in unit["new_prs"]])
            return '```json\n{"joins": [], "new_clusters": [], "standalone": []}\n```'
        monkeypatch.setattr(cluster_driver, "run_assign_agent", assign_agent)
        stage = cluster_pass.assign(store, 3, 1, cluster_pass._Outage(), None)
        assert stage.attempted == 3 and calls == [[1, 2, 3]]


class TestRun:
    @pytest.fixture(autouse=True)
    def _no_analysis(self, monkeypatch):
        monkeypatch.setattr(analyze_clusters, "run",
                            lambda *a, **k: analyze_clusters.AnalyzeRun(0, 0, 0, 0, None))

    def test_a_full_pass_exits_zero(self, store, monkeypatch):
        _pr(store, 1)
        monkeypatch.setattr(cluster_driver, "run_summarize_agent", _summarizer([]))
        monkeypatch.setattr(cluster_driver, "run_assign_agent", lambda *a, **k:
                            '```json\n{"joins": [], "new_clusters": [], "standalone": [1]}\n```')
        assert cluster_pass.run(store, limit=5, analyze_limit=0, concurrency=1) == 0
        assert store.load_pr(1).section("cluster") is not None

    def test_an_agent_outage_stops_the_pass(self, store, monkeypatch):
        for n in range(1, 4):
            _pr(store, n)
        monkeypatch.setattr(cluster_pass, "SUMMARY_BATCH", 1)
        calls: list[int] = []

        def down(batch, on_event=None):
            calls.append(batch[0]["pr"])
            raise headless_agent.AgentUnavailable("claude: command not found")
        monkeypatch.setattr(cluster_driver, "run_summarize_agent", down)
        monkeypatch.setattr(cluster_driver, "run_assign_agent",
                            lambda *a, **k: pytest.fail("assign should not run"))
        rc = cluster_pass.run(store, limit=5, analyze_limit=5, concurrency=1)
        assert rc == cluster_pass.EXIT_AGENT_UNAVAILABLE
        assert calls == [1]

    def test_a_spent_usage_limit_is_not_an_outage(self, store, monkeypatch):
        _pr(store, 1)

        def spent(batch, on_event=None):
            raise headless_agent.CapacityExhausted("usage limit reached", None)
        monkeypatch.setattr(cluster_driver, "run_summarize_agent", spent)
        rc = cluster_pass.run(store, limit=5, analyze_limit=0, concurrency=1)
        assert rc == cluster_pass.EXIT_LIMIT

    def test_a_closed_capacity_gate_defers_the_pass(self, store, monkeypatch):
        _pr(store, 1)

        def paused(batch, on_event=None):
            raise capacity.CapacityPaused(capacity.Decision(False, "daytime cap", None))
        monkeypatch.setattr(cluster_driver, "run_summarize_agent", paused)
        assert cluster_pass.run(store, limit=5, analyze_limit=0, concurrency=1) == 0

    def test_every_agent_failing_is_a_fault(self, store, monkeypatch):
        _pr(store, 1)

        def broken(batch, on_event=None):
            raise RuntimeError("agent crashed")
        monkeypatch.setattr(cluster_driver, "run_summarize_agent", broken)
        rc = cluster_pass.run(store, limit=5, analyze_limit=0, concurrency=1)
        assert rc == cluster_pass.EXIT_FAULT


def test_the_assign_index_carries_the_canonical_prompt(store, tmp_path, monkeypatch):
    monkeypatch.setattr(cluster_driver, "ASSIGN_UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(cluster_driver, "ASSIGN_OUT_DIR", tmp_path / "out")
    _pr(store, 1, summarized=True)
    cluster_driver.assign_units(store)
    index = json.loads((tmp_path / "units" / "index.json").read_text())
    assert index["prompt"] == cluster_driver.assign_prompt()
    assert "__UNIT_PATH__" in index["prompt"] and "__REPO__" not in index["prompt"]
