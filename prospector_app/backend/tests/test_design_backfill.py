from __future__ import annotations

import pytest

from pipeline import reviewers
from pipeline import store as S
from prospector_app.backend import data
from prospector_app.backend import design_backfill
from prospector_app.backend import verify_worker
from prospector_app.backend.tests.test_autohunt import HEAD, _clean_merge_pr


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_WORKER_ID", "mac")
    monkeypatch.setattr(verify_worker, "design_base", lambda st: ("b" * 40, tmp_path, 1))
    for n, pain in ((1, 0.0), (2, 5.0), (3, 1.0)):
        st.save_pr(_clean_merge_pr(n, pain=pain))
    data.refresh()
    return st


def test_merge_ready_prs_come_first_then_the_hunt_order(store, monkeypatch):
    monkeypatch.setattr(design_backfill.automation, "classify",
                        lambda pr: {"bucket": "merge-ready" if pr.number == 1 else "other"})
    assert design_backfill.candidates() == [1, 2, 3]


def test_reviewed_heads_and_other_claims_are_left_out(store):
    store.save_design_review(1, {"kind": "review", "reviewed_sha": HEAD, "findings": [],
                                 "checks": [], "extra": {"rubric": reviewers.DESIGN_RUBRIC}}, head_sha=HEAD)
    store.claim_design_run(2, host="studio", stale_after=3600)
    data.refresh()
    assert design_backfill.candidates() == [3]


def test_stops_at_the_limit(store, monkeypatch):
    ran: list[tuple[int, str]] = []
    monkeypatch.setattr(design_backfill, "candidates", lambda: [1, 2, 3])
    monkeypatch.setattr(verify_worker, "run_design",
                        lambda n, trigger: ran.append((n, trigger)) or True)
    assert design_backfill.run(2) == 0
    assert ran == [(1, "control"), (2, "control")]


def test_stops_after_three_failures_in_a_row(store, monkeypatch):
    outcomes = iter([False, True, None, False, False, False, True])
    monkeypatch.setattr(design_backfill, "candidates", lambda: [1, 2, 3, 4, 5, 6, 7])
    monkeypatch.setattr(verify_worker, "run_design", lambda n, trigger: next(outcomes))
    assert design_backfill.run(10) == 1
    assert next(outcomes) is True


def test_refuses_without_a_base_pin(store, monkeypatch, capsys):
    monkeypatch.setattr(verify_worker, "design_base", lambda st: None)
    assert design_backfill.run(5) == 1
    assert "no verify base pin" in capsys.readouterr().out

