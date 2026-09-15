"""The re-review hunter asks a reviewer for the verdict a head never got, once
per head, a bounded number per pass and per day, as the bot."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import store as S
from pipeline.storekit import now as _now
from pipeline.testsupport import greptile_entry, reviews_section
from prospector_app.backend import data, rereview_hunt

HEAD = "a" * 40
OLD = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()


def _pr(n: int, *, reviewed_sha: str | None = "0" * 40, updated: str = OLD,
        ci: str = "passing", mergeable: bool = True) -> dict:
    return {"pr": n,
            "meta": {"title": f"pr {n}", "state": "open", "head_sha": HEAD, "updated_at": updated},
            "signals": {"ci": ci, "mergeable": mergeable, "checked_at": _now(),
                        "against_head_sha": HEAD},
            "reviews": reviews_section(HEAD, _now(), greptile=greptile_entry(4, reviewed_sha)),
            "drift": {"state": "applicable", "checked_at": _now(), "against_head_sha": HEAD}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_FIX_HUNT_REREVIEW", "1")
    monkeypatch.delenv("TRIAGE_REREVIEW_BUDGET", raising=False)
    return st


def _wire(monkeypatch, token: str | None = "tok", status: str = "executed"):
    posted: list[tuple[int, str]] = []
    scheduled: list[int] = []
    monkeypatch.setattr(rereview_hunt.executor, "mint_bot_token", lambda: token)
    monkeypatch.setattr(rereview_hunt.executor, "retrigger_review",
                        lambda n, rid, *, token, dry_run: posted.append((n, rid)) or {"status": status})
    monkeypatch.setattr(rereview_hunt.review_refresh, "capture", lambda n, rid: object())
    monkeypatch.setattr(rereview_hunt.review_refresh, "schedule",
                        lambda n, rid, baseline: scheduled.append(n))
    return posted, scheduled


def test_a_stale_verdict_on_an_old_head_is_asked_for(store, monkeypatch):
    store.save_pr(_pr(1)); data.refresh()
    posted, scheduled = _wire(monkeypatch)
    assert rereview_hunt.request_rereviews() == [1]
    assert posted == [(1, "greptile")] and scheduled == [1]
    bookings = rereview_hunt._requests(datetime.now(timezone.utc) - timedelta(days=1))
    assert bookings and bookings[0]["stats"]["head_sha"] == HEAD


def test_a_head_is_asked_about_once(store, monkeypatch):
    store.save_pr(_pr(1)); data.refresh()
    posted, _ = _wire(monkeypatch)
    rereview_hunt.request_rereviews()
    assert rereview_hunt.request_rereviews() == []
    assert len(posted) == 1


def test_a_fresh_verdict_a_young_head_or_red_ci_is_left_alone(store, monkeypatch):
    store.save_pr(_pr(1, reviewed_sha=HEAD))
    store.save_pr(_pr(2, updated=datetime.now(timezone.utc).isoformat()))
    store.save_pr(_pr(3, ci="failing"))
    store.save_pr(_pr(4, mergeable=False))
    data.refresh()
    assert rereview_hunt.candidates() == []


def test_each_pass_and_each_day_are_bounded(store, monkeypatch):
    for n in range(1, 9):
        store.save_pr(_pr(n))
    data.refresh()
    posted, _ = _wire(monkeypatch)
    assert rereview_hunt.request_rereviews(limit=3) == [1, 2, 3]
    monkeypatch.setenv("TRIAGE_REREVIEW_BUDGET", "4")
    assert rereview_hunt.request_rereviews(limit=3) == [4]
    assert rereview_hunt.request_rereviews(limit=3) == []


def test_nothing_is_posted_without_a_bot_token_or_with_the_lane_off(store, monkeypatch):
    store.save_pr(_pr(1)); data.refresh()
    posted, _ = _wire(monkeypatch, token=None)
    assert rereview_hunt.request_rereviews() == [] and posted == []
    posted, _ = _wire(monkeypatch)
    monkeypatch.setenv("TRIAGE_FIX_HUNT_REREVIEW", "0")
    assert rereview_hunt.request_rereviews() == [] and posted == []
