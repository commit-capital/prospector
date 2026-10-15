"""The hunters' next picks, read without picking."""
from __future__ import annotations

import pytest

from pipeline import design_review, gates
from pipeline import store as S
from prospector_app.backend import data, fix_worker, verify_worker


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    for n in (9, 3, 5, 7):
        st.save_pr({"pr": n, "meta": {"title": f"pr {n}", "state": "open", "head_sha": "a" * 40}})
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    return st


def test_verify_hunter_lists_its_security_pool_in_pick_order(store, monkeypatch):
    monkeypatch.setattr(gates, "blocked_on_security", lambda pr: pr.number != 7)
    monkeypatch.setattr(verify_worker, "auto_verifiable", lambda pr: False)
    monkeypatch.setattr(design_review, "huntable", lambda pr: pr.number == 7)
    picks = verify_worker.hunt_candidates(limit=2)
    assert picks == {"security": [3, 5], "verify": [], "design": [7]}
    assert verify_worker.next_auto(frozenset({"security"})) == ("security", 3)


def test_fix_hunter_lists_its_picks_in_pick_order(store, monkeypatch):
    monkeypatch.setattr(fix_worker, "auto_fixable",
                        lambda pr, budget_ok=True: ("rebase", None) if pr.number != 5 else None)
    assert fix_worker.hunt_candidates(limit=2) == [("rebase", 3), ("rebase", 7)]
