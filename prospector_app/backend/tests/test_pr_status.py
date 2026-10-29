"""pr_status: what a PR page's status block shows beyond its standing."""
from __future__ import annotations

import pytest

from pipeline import pr_outcomes, storekit
from pipeline import store as S
from pipeline.storekit import now as _now
from pipeline.testsupport import greptile_entry, reviews_section
from prospector_app.backend import data, issue_data, pr_status, service

HEAD = "a" * 40


def _rec(n: int = 7, *, ci: str = "passing", greptile: int = 5) -> dict:
    return {"pr": n,
            "meta": {"title": "Fix the thing", "author": "al", "state": "open", "head_sha": HEAD,
                     "updated_at": "2026-06-01T00:00:00+00:00"},
            "signals": {"ci": ci, "mergeable": True, "checked_at": _now(),
                        "against_head_sha": HEAD},
            "reviews": reviews_section(HEAD, _now(), greptile=greptile_entry(greptile, HEAD)),
            "drift": {"state": "applicable", "checked_at": _now(), "against_head_sha": HEAD}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(service, "changed_paths", lambda pr: ["src/a.ts"])
    monkeypatch.setattr(issue_data, "issues", lambda: {})
    monkeypatch.setattr(issue_data, "runs", lambda: [])
    return st


def _load(store, rec: dict):
    store.save_pr(rec)
    data.refresh()
    return store.load_pr(rec["pr"])


def _stage(stages: list[pr_status.Stage], key: str) -> str:
    return next(s["status"] for s in stages if s["key"] == key)


def test_the_first_unfinished_stage_is_current(store):
    pr = _load(store, _rec(greptile=2))
    stages = pr_status.stages(pr, {"bucket": "hunt", "state": "moving"})
    assert _stage(stages, "ci") == "done"
    assert _stage(stages, "review") == "failed"
    after = [s["status"] for s in stages[[s["key"] for s in stages].index("review") + 1:]]
    assert set(after) <= {"todo", "failed"}
    assert "current" not in after


def test_a_handed_back_fix_pr_marks_its_blocker_failed(store):
    pr = _load(store, _rec())
    stages = pr_status.stages(pr, {"bucket": "handed-back", "state": "stalled"},
                              blocked_by="Greptile")
    assert _stage(stages, "review") == "failed"


def test_tried_lists_this_prs_outcomes_newest_first(store):
    def row(n: int, at: str, stage: str) -> dict:
        return {"phase": "design-review:run", "pr": n, "started": at, "finished": at,
                "outcome": pr_outcomes.design(n, HEAD, stage, f"{stage} words")}
    store.append_run(row(7, "2026-10-20T00:00:00+00:00", "error"))
    store.append_run(row(8, "2026-10-21T00:00:00+00:00", "error"))
    store.append_run(row(7, "2026-10-22T00:00:00+00:00", "agent"))
    pr = _load(store, _rec())
    got = pr_status.tried(pr)
    assert [(t["stage"], t["at"]) for t in got] == [
        ("agent", "2026-10-22T00:00:00+00:00"), ("error", "2026-10-20T00:00:00+00:00")]


def test_the_seed_names_the_pr_its_blocker_and_the_ask(store):
    pr = _load(store, _rec())
    tried = [{**pr_outcomes.design(7, HEAD, "error", "the run crashed"),
              "at": "2026-10-22T00:00:00+00:00"}]
    seed = pr_status.seed(pr, {"state": "stalled", "reason": "Greptile scores it 4/5"}, tried)
    assert "#7" in seed and "Greptile scores it 4/5" in seed
    assert "design-review error machine: the run crashed" in seed
    assert pr_status.ASK in seed


def test_a_pr_with_nothing_tried_still_seeds(store):
    pr = _load(store, _rec())
    seed = pr_status.seed(pr, {"state": "stalled", "reason": "needs a decision"}, [])
    assert pr_status.ASK in seed


def test_the_seed_is_only_for_a_stalled_pr(store, monkeypatch):
    pr = _load(store, _rec())
    monkeypatch.setattr(pr_status.automation, "classify",
                        lambda p: {"bucket": "hunt", "state": "moving", "reason": "next pick"})
    assert pr_status.status(pr)["seed"] is None
    monkeypatch.setattr(pr_status.automation, "classify",
                        lambda p: {"bucket": "needs-human", "state": "stalled", "reason": "x"})
    assert pr_status.status(pr)["seed"]


def test_a_ledger_row_without_an_outcome_is_not_tried(store):
    store.append_run({"phase": "ingest", "started": _now(), "finished": _now()})
    pr = _load(store, _rec())
    assert pr_status.tried(pr) == []


@pytest.fixture
def lanes(monkeypatch):
    def setup(lane: str | None, order: list[int], runs: list[dict]) -> list[str]:
        asked: list[str] = []

        def lane_order(name: str) -> list[int]:
            asked.append(name)
            return order
        monkeypatch.setattr(pr_status, "_lane", lambda pr, standing: lane)
        monkeypatch.setattr(pr_status, "lane_order", lane_order)
        monkeypatch.setattr(pr_status.data, "prs", lambda: {7: object(), 42: object()})
        monkeypatch.setattr(pr_status.automation, "classify", lambda pr: {"bucket": "x"})
        monkeypatch.setattr(pr_status.data, "runs",
                            lambda since=None: [storekit.parse_run(r) for r in runs])
        return asked
    return setup


def _run(phase: str) -> dict:
    return {"phase": phase, "started": _now(), "finished": _now()}


def test_position_and_eta_come_from_the_pick_order_and_the_lane_rate(lanes):
    lanes("security", [3, 7, 9], [_run("security:review-one") for _ in range(12)])
    assert pr_status.next_step(7, days=1) == {"lane": "security", "position": 2, "of": 3,
                                              "eta_hours": 4.0}


def test_a_lane_with_no_runs_has_no_eta(lanes):
    lanes("design", [7], [])
    step = pr_status.next_step(7)
    assert step is not None and step["eta_hours"] is None


def test_a_pr_in_no_pool_has_no_next_step(lanes):
    lanes("security", [3], [])
    assert pr_status.next_step(42) is None


def test_a_pr_no_lane_takes_orders_no_queue(lanes):
    asked = lanes(None, [7], [])
    assert pr_status.next_step(7) is None and asked == []


def test_only_the_lane_the_standing_names_is_ordered(lanes):
    asked = lanes("fix", [5, 7], [_run("fix:single") for _ in range(24)])
    assert pr_status.next_step(7, days=1) == {"lane": "fix", "position": 2, "of": 2,
                                              "eta_hours": 2.0}
    assert asked == ["fix"]


@pytest.mark.parametrize("bucket, lane", [("hunt", "fix"), ("budgeted", "fix")])
def test_a_hunter_pick_waits_in_the_fix_lane(store, monkeypatch, bucket, lane):
    pr = _load(store, _rec())
    monkeypatch.setattr(pr_status.design_review, "huntable", lambda p: True)
    assert pr_status._lane(pr, {"bucket": bucket}) == lane


def test_an_unreviewed_head_waits_in_the_design_lane(store, monkeypatch):
    pr = _load(store, _rec())
    monkeypatch.setattr(pr_status.gates, "blocked_on_security", lambda p: False)
    monkeypatch.setattr(pr_status.verify_worker, "auto_verifiable", lambda p: False)
    monkeypatch.setattr(pr_status.design_review, "huntable", lambda p: True)
    assert pr_status._lane(pr, {"bucket": "waiting"}) == "design"
