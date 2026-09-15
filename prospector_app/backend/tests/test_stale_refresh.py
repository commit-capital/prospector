"""stale_refresh: merge candidates whose facts went stale are re-ingested on
a cadence, a bounded batch at a time, on a worker machine."""
from __future__ import annotations

import pytest

from pipeline import store as S
from pipeline.storekit import now as _now
from pipeline.testsupport import reviews_section
from prospector_app.backend import data, stale_refresh

HEAD = "a" * 40


def _pr(n: int, disposition: str = "merge", head: str = HEAD, stamped: str = HEAD) -> dict:
    now = _now()
    return {"pr": n,
            "meta": {"title": f"pr {n}", "state": "open", "head_sha": head, "checked_at": now},
            "signals": {"ci": "passing", "mergeable": True, "checked_at": now,
                        "against_head_sha": stamped},
            "reviews": reviews_section(stamped, now),
            "drift": {"state": "applicable", "checked_at": now, "against_head_sha": stamped},
            "analysis": {"disposition": disposition, "rationale": "r", "checked_at": now,
                         "against_head_sha": head}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    return st


def test_only_stale_merge_candidates_are_selected(store):
    store.save_pr(_pr(1))                                   # current
    store.save_pr(_pr(2, head="b" * 40, stamped=HEAD))      # merge, stale
    store.save_pr(_pr(3, disposition="request-changes", head="b" * 40, stamped=HEAD))
    data.refresh()
    assert stale_refresh.stale_merge_candidates() == [2]


def test_refresh_runs_ingest_over_a_bounded_batch(store, monkeypatch):
    for n in range(1, 6):
        store.save_pr(_pr(n, head="b" * 40, stamped=HEAD))
    data.refresh()
    seen: list[list[int]] = []
    monkeypatch.setattr(stale_refresh.ingest, "refresh_prs",
                        lambda st, numbers: seen.append(list(numbers)) or [])
    assert stale_refresh.refresh_stale(limit=3) == [1, 2, 3]
    assert seen == [[1, 2, 3]]


def test_nothing_stale_calls_nothing(store, monkeypatch):
    store.save_pr(_pr(1))
    data.refresh()
    monkeypatch.setattr(stale_refresh.ingest, "refresh_prs",
                        lambda st, numbers: pytest.fail("ingest ran with nothing stale"))
    assert stale_refresh.refresh_stale() == []


def test_start_only_on_a_worker_machine(monkeypatch):
    monkeypatch.delenv("TRIAGE_VERIFY_WORKER", raising=False)
    monkeypatch.delenv("TRIAGE_FIX_WORKER", raising=False)
    assert stale_refresh.start() is False
