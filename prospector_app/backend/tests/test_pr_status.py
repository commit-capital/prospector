"""pr_status: what a PR page's status block shows beyond its standing."""
from __future__ import annotations

import pytest

from pipeline import pr_outcomes
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
