"""Backend-owned reconciliation after an asynchronous Greptile re-trigger."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from review_cockpit.backend import app as appmod
from review_cockpit.backend import data
from review_cockpit.backend import review_refresh


def _baseline(*, version: str | None = "old", score: int | None = 4):
    return review_refresh.Baseline(
        version=version, previous_score=score,
        captured_at=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc))


def test_waits_for_changed_summary_then_targeted_ingests(monkeypatch):
    feedback = iter([
        {"score": 4, "version": "old"},
        {"score": 5, "version": "new", "updated_at": "2026-07-22T12:01:00Z"},
    ])
    monkeypatch.setattr(review_refresh.greptile, "fetch_greptile_feedback",
                        lambda n: next(feedback))
    sentinel_store = object()
    monkeypatch.setattr(data, "store", lambda: sentinel_store)
    calls = []
    monkeypatch.setattr(review_refresh.ingest, "refresh_prs",
                        lambda store, prs: calls.append((store, prs)))
    refreshed = []
    monkeypatch.setattr(data, "refresh", lambda: refreshed.append(True))

    assert review_refresh.wait_and_refresh(7, _baseline(), attempts=2, interval=0) is True
    assert calls == [(sentinel_store, [7])]
    assert refreshed == [True]


def test_no_prior_score_accepts_first_summary(monkeypatch):
    monkeypatch.setattr(review_refresh.greptile, "fetch_greptile_feedback",
                        lambda n: {"score": 5, "version": "first"})
    monkeypatch.setattr(data, "store", lambda: object())
    calls = []
    monkeypatch.setattr(review_refresh.ingest, "refresh_prs",
                        lambda store, prs: calls.append(prs))
    monkeypatch.setattr(data, "refresh", lambda: None)

    baseline = _baseline(version=None, score=None)
    assert review_refresh.wait_and_refresh(7, baseline, attempts=1, interval=0) is True
    assert calls == [[7]]


def test_failed_baseline_read_does_not_accept_old_stored_score(monkeypatch):
    before = _baseline(version=None, score=4)
    old = (before.captured_at - timedelta(minutes=5)).isoformat()
    monkeypatch.setattr(review_refresh.greptile, "fetch_greptile_feedback",
                        lambda n: {"score": 4, "version": "old", "updated_at": old})
    monkeypatch.setattr(review_refresh.ingest, "refresh_prs",
                        lambda store, prs: (_ for _ in ()).throw(AssertionError("must not ingest")))

    assert review_refresh.wait_and_refresh(7, before, attempts=1, interval=0) is False


def test_retrigger_route_schedules_backend_refresh_after_post_lands(monkeypatch):
    baseline = _baseline()
    monkeypatch.setattr(appmod.executor, "mint_bot_token", lambda: "token")
    monkeypatch.setattr(appmod.review_refresh, "capture", lambda n: baseline)
    monkeypatch.setattr(appmod.executor, "retrigger_greptile",
                        lambda n, token, dry_run: {"pr": n, "status": "executed"})
    scheduled = []
    monkeypatch.setattr(appmod.review_refresh, "schedule",
                        lambda n, captured: scheduled.append((n, captured)))

    r = TestClient(appmod.app).post("/api/greptile/retrigger/pr/7?dry_run=false")
    assert r.status_code == 200 and r.json()["status"] == "executed"
    assert scheduled == [(7, baseline)]


def test_retrigger_route_does_not_schedule_dry_run(monkeypatch):
    monkeypatch.setattr(appmod.executor, "retrigger_greptile",
                        lambda n, token, dry_run: {"pr": n, "status": "dry-run"})
    monkeypatch.setattr(appmod.review_refresh, "capture",
                        lambda n: (_ for _ in ()).throw(AssertionError("must not capture")))
    scheduled = []
    monkeypatch.setattr(appmod.review_refresh, "schedule", lambda *args: scheduled.append(args))

    r = TestClient(appmod.app).post("/api/greptile/retrigger/pr/7?dry_run=true")
    assert r.status_code == 200 and r.json()["status"] == "dry-run"
    assert scheduled == []
