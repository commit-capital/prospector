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


def test_fix_hunter_leads_with_a_fix_while_a_slot_is_free(store, monkeypatch):
    monkeypatch.setattr(fix_worker, "auto_fixable",
                        lambda pr, budget_ok=True: ("fix", None) if pr.number == 9 else ("rebase", None))
    monkeypatch.setattr(fix_worker, "_auto_in_flight", lambda action: 0)
    monkeypatch.setenv("TRIAGE_FIX_HUNT_LIMIT", "3")
    picks = fix_worker.hunt_candidates(limit=3)
    assert picks[0] == ("fix", 9)
    monkeypatch.setattr(fix_worker, "_auto_in_flight", lambda action: 3)
    assert ("fix", 9) not in fix_worker.hunt_candidates(limit=5)


def test_no_design_picks_while_design_review_is_off(store, monkeypatch):
    monkeypatch.setattr(gates, "blocked_on_security", lambda pr: False)
    monkeypatch.setattr(verify_worker, "auto_verifiable", lambda pr: False)
    monkeypatch.setattr(design_review, "huntable", lambda pr: True)
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "off")
    assert verify_worker.hunt_candidates()["design"] == []


def test_a_maintainer_s_coverage_retry_waits_behind_every_unreviewed_head(store, monkeypatch):
    monkeypatch.setattr(gates, "blocked_on_security", lambda pr: False)
    monkeypatch.setattr(verify_worker, "auto_verifiable", lambda pr: False)
    monkeypatch.setattr(design_review, "huntable", lambda pr: True)
    monkeypatch.setattr(design_review, "retrying", lambda pr: pr.number == 3)
    monkeypatch.setattr(gates, "priority_author", lambda author, association: True)
    assert verify_worker.hunt_candidates()["design"] == [5, 7, 9, 3]
    assert verify_worker.next_auto(frozenset({"design"})) == ("design", 5)


def test_a_maintainer_s_coverage_retry_does_not_take_the_lane_first(store, monkeypatch):
    monkeypatch.setattr(gates, "blocked_on_security", lambda pr: pr.number == 9)
    monkeypatch.setattr(verify_worker, "auto_verifiable", lambda pr: False)
    monkeypatch.setattr(design_review, "huntable", lambda pr: pr.number == 3)
    monkeypatch.setattr(design_review, "retrying", lambda pr: True)
    monkeypatch.setattr(gates, "priority_author",
                        lambda author, association: author == "maintainer")
    store.save_pr({"pr": 3, "meta": {"title": "pr 3", "state": "open", "head_sha": "a" * 40,
                                     "author": "maintainer"}})
    data.refresh()
    monkeypatch.setattr(verify_worker, "_last_auto_lane", "design")
    assert verify_worker.next_auto(frozenset({"security", "design"})) == ("security", 9)
