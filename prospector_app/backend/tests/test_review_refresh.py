"""Backend-owned reconciliation after an asynchronous reviewer re-trigger."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from prospector_app.backend import app as appmod
from prospector_app.backend import data
from prospector_app.backend import review_refresh


def _baseline(*, version: str | None = "old"):
    return review_refresh.Baseline(
        version=version, captured_at=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc))


def _entry(score: int, observed: str = "2026-07-22T12:01:00Z") -> dict:
    return {"kind": "review", "score": score, "reviewed_sha": "h", "findings": [],
            "checks": [], "extra": {}, "observed_at": observed}


def test_waits_for_changed_entry_then_targeted_ingests(monkeypatch):
    entries = iter([_entry(4, "2026-07-22T11:00:00Z"), _entry(5)])
    monkeypatch.setattr(review_refresh, "live_entry", lambda n, rid: next(entries))
    sentinel_store = object()
    monkeypatch.setattr(data, "store", lambda: sentinel_store)
    calls = []
    monkeypatch.setattr(review_refresh.ingest, "refresh_prs",
                        lambda store, prs: calls.append((store, prs)))
    refreshed = []
    monkeypatch.setattr(data, "refresh", lambda: refreshed.append(True))

    first = review_refresh.reviewers.version(review_refresh.reviewers.GREPTILE,
                                             _entry(4, "2026-07-22T11:00:00Z"))
    assert review_refresh.wait_and_refresh(7, "greptile", _baseline(version=first),
                                           attempts=2, interval=0) is True
    assert calls == [(sentinel_store, [7])]
    assert refreshed == [True]


def test_no_prior_entry_accepts_first_one(monkeypatch):
    monkeypatch.setattr(review_refresh, "live_entry", lambda n, rid: _entry(5))
    monkeypatch.setattr(data, "store", lambda: object())
    calls = []
    monkeypatch.setattr(review_refresh.ingest, "refresh_prs",
                        lambda store, prs: calls.append(prs))
    monkeypatch.setattr(data, "refresh", lambda: None)

    assert review_refresh.wait_and_refresh(7, "greptile", _baseline(version=None),
                                           attempts=1, interval=0) is True
    assert calls == [[7]]


def test_failed_baseline_read_does_not_accept_an_older_entry(monkeypatch):
    before = _baseline(version=None)
    old = (before.captured_at - timedelta(minutes=5)).isoformat()
    monkeypatch.setattr(review_refresh, "live_entry", lambda n, rid: _entry(4, old))
    monkeypatch.setattr(review_refresh.ingest, "refresh_prs",
                        lambda store, prs: (_ for _ in ()).throw(AssertionError("must not ingest")))

    assert review_refresh.wait_and_refresh(7, "greptile", before, attempts=1, interval=0) is False


def test_capture_reads_the_live_entry_version(monkeypatch):
    monkeypatch.setattr(review_refresh, "live_entry", lambda n, rid: _entry(4))
    b = review_refresh.capture(7, "greptile")
    assert b.version == review_refresh.reviewers.version(review_refresh.reviewers.GREPTILE, _entry(4))
    monkeypatch.setattr(review_refresh, "live_entry", lambda n, rid: None)
    assert review_refresh.capture(7, "greptile").version is None


def test_retrigger_route_schedules_backend_refresh_after_post_lands(monkeypatch):
    baseline = _baseline()
    monkeypatch.setattr(appmod.executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(appmod.review_refresh, "capture", lambda n, rid: baseline)
    monkeypatch.setattr(appmod.executor, "retrigger_review",
                        lambda n, rid, *, token, dry_run: {"pr": n, "reviewer": rid, "status": "executed"})
    scheduled = []
    monkeypatch.setattr(appmod.review_refresh, "schedule",
                        lambda n, rid, captured: scheduled.append((n, rid, captured)))

    r = TestClient(appmod.app).post("/api/reviews/greptile/retrigger/pr/7?dry_run=false")
    assert r.status_code == 200 and r.json()["status"] == "executed"
    assert scheduled == [(7, "greptile", baseline)]


def test_retrigger_route_does_not_schedule_dry_run(monkeypatch):
    monkeypatch.setattr(appmod.executor, "retrigger_review",
                        lambda n, rid, *, token, dry_run: {"pr": n, "reviewer": rid, "status": "dry-run"})
    monkeypatch.setattr(appmod.review_refresh, "capture",
                        lambda n, rid: (_ for _ in ()).throw(AssertionError("must not capture")))
    scheduled = []
    monkeypatch.setattr(appmod.review_refresh, "schedule", lambda *args: scheduled.append(args))

    r = TestClient(appmod.app).post("/api/reviews/greptile/retrigger/pr/7?dry_run=true")
    assert r.status_code == 200 and r.json()["status"] == "dry-run"
    assert scheduled == []
