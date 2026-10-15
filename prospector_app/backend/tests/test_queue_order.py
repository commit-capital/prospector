"""Move-to-top: an operator's priority puts a waiting request first."""
from __future__ import annotations

import pytest

from pipeline import store as S
from prospector_app.backend import data, fix_worker, queue_order, verify_worker


def _pr(n: int, section: str, *, source: str = "operator", status: str = "queued") -> dict:
    req = {"status": status, "queued_at": f"2026-10-0{n}T00:00:00+00:00", "source": source}
    if section == "fix_request":
        req["action"] = "rebase"
    return {"pr": n, "meta": {"title": f"pr {n}", "state": "open", "head_sha": "a" * 40},
            section: req}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    return st


def test_verify_order_takes_a_moved_item_first(store):
    for n in (1, 2, 3):
        store.save_pr(_pr(n, "verify_request"))
    data.refresh()
    assert verify_worker.next_queued() == 1
    queue_order.top("verify", 3)
    data.refresh()
    assert verify_worker.next_queued() == 3


def test_fix_order_takes_a_moved_item_first(store):
    for n in (1, 2):
        store.save_pr(_pr(n, "fix_request"))
    data.refresh()
    assert fix_worker._oldest("queued") == 1
    queue_order.top("fix", 2)
    data.refresh()
    assert fix_worker._oldest("queued") == 2


def test_a_moved_auto_pick_beats_an_operator_pick(store):
    store.save_pr(_pr(1, "verify_request"))
    store.save_pr(_pr(2, "verify_request", source="auto"))
    data.refresh()
    queue_order.top("verify", 2)
    data.refresh()
    assert verify_worker.next_queued() == 2


def test_top_stacks_in_click_order(store):
    for n in (1, 2, 3):
        store.save_pr(_pr(n, "verify_request"))
    data.refresh()
    first = queue_order.top("verify", 2)["priority"]
    second = queue_order.top("verify", 3)["priority"]
    assert second > first
    data.refresh()
    assert verify_worker.next_queued() == 3


def test_top_refuses_an_item_that_is_not_waiting(store):
    store.save_pr(_pr(1, "verify_request", status="running"))
    data.refresh()
    with pytest.raises(ValueError):
        queue_order.top("verify", 1)


def _drain_order(pick, n_items: int, section: str, store) -> list[int]:
    out = []
    for _ in range(n_items):
        n = pick()
        out.append(n)
        rec = store.load_pr(n).raw
        rec[section]["status"] = "cancelled"
        store.save_pr(rec)
        data.refresh()
    return out


def test_verify_queue_view_lists_in_the_order_the_worker_runs(store):
    from prospector_app.backend import verify_queue
    store.save_pr(_pr(1, "verify_request", source="auto"))
    store.save_pr(_pr(2, "verify_request"))
    store.save_pr(_pr(3, "verify_request", source="auto"))
    data.refresh()
    queue_order.top("verify", 3)
    listed = [e["pr"] for e in verify_queue.queue_entries()]
    assert listed == _drain_order(verify_worker.next_queued, 3, "verify_request", store) == [3, 2, 1]


def test_fix_queue_view_lists_in_the_order_the_worker_runs(store):
    from prospector_app.backend import fix_queue
    store.save_pr(_pr(1, "fix_request", source="auto"))
    store.save_pr(_pr(2, "fix_request"))
    data.refresh()
    listed = [e["pr"] for e in fix_queue.queue_entries()]
    assert listed == _drain_order(lambda: fix_worker._oldest("queued"), 2, "fix_request", store) == [2, 1]
