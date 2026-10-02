"""pr_watch: a worker pass records the PRs GitHub reports new, pushed to,
reopened or closed since the last pass, and wakes the threat scan for them."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import ingest
from pipeline import store as S
from pipeline.storekit import now as _now
from prospector_app.backend import data, pr_watch, threat_refresh

NOW = datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)


def _gh(n: int, head: str, *, state: str = "open", updated: str = "2026-10-02T17:55:00Z",
        merged_at: str | None = None) -> dict:
    return {"number": n, "title": f"PR {n}", "body": None, "user": {"login": "dev"},
            "state": state, "draft": False, "head": {"sha": head}, "base": {"ref": "main"},
            "html_url": f"https://github.com/o/r/pull/{n}", "created_at": "2026-10-01T00:00:00Z",
            "updated_at": updated, "merged_at": merged_at}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(ingest, "load_issue_links", lambda **_: {})
    monkeypatch.setattr(ingest.live_prs, "fetch", lambda prs, **kw: ({}, set()))
    monkeypatch.setattr(ingest.review_fetch, "fetch_feeds", lambda numbers, **kw: {})
    return st


@pytest.fixture
def woke(monkeypatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(threat_refresh, "wake", lambda: calls.append(True))
    return calls


def test_a_pass_records_new_pushed_and_closed_prs_and_wakes_the_threat_scan(
        store, woke, monkeypatch):
    ingest.upsert_pr(store, _gh(5001, "old"))
    ingest.upsert_pr(store, _gh(5002, "s2"))
    store.append_run({"phase": "ingest", "started": "2026-10-02T17:00:00+00:00",
                      "finished": "2026-10-02T17:10:00+00:00", "stats": {}})
    asked: list[str] = []

    def listing(since: str) -> list[dict]:
        asked.append(since)
        return [_gh(6001, "n1"), _gh(5001, "pushed"),
                _gh(5002, "s2", state="closed", merged_at="2026-10-02T17:50:00Z")]
    monkeypatch.setattr(ingest, "fetch_updated_prs", listing)
    data.refresh()

    stats = pr_watch.watch_once(NOW)
    assert stats == {"listed": 3, "upserted": 2, "closed": 1}
    assert asked == ["2026-10-02T16:55:00Z"]
    assert store.load_pr(6001).head_sha == "n1"
    assert store.load_pr(5001).head_sha == "pushed"
    assert store.load_pr(5002).state == "merged"
    assert store.latest_run(pr_watch.PHASE).raw["stats"] == stats
    assert woke == [True]


def test_a_pass_with_nothing_new_records_nothing_and_wakes_nothing(store, woke, monkeypatch):
    ingest.upsert_pr(store, _gh(5001, "h"))
    store.append_run({"phase": "ingest", "started": "2026-10-02T17:00:00+00:00",
                      "finished": "2026-10-02T17:10:00+00:00", "stats": {}})
    monkeypatch.setattr(ingest, "fetch_updated_prs", lambda since: [_gh(5001, "h")])
    data.refresh()
    assert pr_watch.watch_once(NOW) == {"listed": 1, "upserted": 0, "closed": 0}
    assert woke == []


def test_with_no_ingest_on_record_a_pass_lists_the_open_prs(store, woke, monkeypatch):
    monkeypatch.setattr(ingest, "fetch_updated_prs",
                        lambda since: pytest.fail("needs a watermark"))
    monkeypatch.setattr(ingest, "fetch_open_prs", lambda max_n=None: [_gh(7001, "a")])
    data.refresh()
    assert pr_watch.watch_once(NOW) == {"listed": 1, "upserted": 1, "closed": 0}
    assert store.load_pr(7001) is not None


def test_a_pass_another_machine_just_made_is_skipped(store, woke, monkeypatch):
    just = (NOW - timedelta(minutes=3)).isoformat()
    store.append_run({"phase": pr_watch.PHASE, "started": just, "finished": just,
                      "stats": {}})
    monkeypatch.setattr(ingest, "fetch_updated_prs", lambda since: pytest.fail("listed"))
    monkeypatch.setattr(ingest, "fetch_open_prs", lambda max_n=None: pytest.fail("listed"))
    assert pr_watch.watch_once(NOW) is None


def test_the_watermark_is_the_later_pass_less_the_overlap():
    assert pr_watch.since("2026-10-02T17:40:00+00:00", "2026-10-02T12:00:00+00:00") \
        == "2026-10-02T17:35:00Z"
    assert pr_watch.since(None, "2026-10-02T12:00:00.123456+00:00") == "2026-10-02T11:55:00Z"
    assert pr_watch.since(None, None) is None


def test_the_cadence_needs_a_worker_lane_and_the_switch(monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_WORKER", "1")
    monkeypatch.setenv("TRIAGE_PR_WATCH", "0")
    assert not pr_watch.enabled()
    monkeypatch.delenv("TRIAGE_PR_WATCH")
    assert pr_watch.enabled()
    monkeypatch.delenv("TRIAGE_VERIFY_WORKER")
    monkeypatch.delenv("TRIAGE_FIX_WORKER", raising=False)
    assert not pr_watch.enabled()


def test_the_ledger_stamps_read_back_as_the_watermark(store, woke, monkeypatch):
    store.append_run({"phase": pr_watch.PHASE, "started": _now(), "finished": _now(),
                      "stats": {}})
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    seen: list[str] = []
    monkeypatch.setattr(ingest, "fetch_updated_prs", lambda since: seen.append(since) or [])
    data.refresh()
    pr_watch.watch_once(later)
    assert seen and seen[0].endswith("Z")
