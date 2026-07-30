"""activity_backfill reconstructs activity-log entries for bot PR closes that
landed upstream but have no landed close in the log, stamped with the real
upstream close instant and marked `backfilled`. GitHub reads are monkeypatched;
the activity log and PR store are real (throwaway)."""
from __future__ import annotations

import json
import types

from pipeline.store import Store
from prospector_app.backend import activity
from prospector_app.backend import activity_backfill as ab
from prospector_app.backend import data


def _gh(payloads: dict[str, str]):
    """A fake safety_guard.run: matches the first `payloads` key found in the
    joined argv and returns its canned stdout. Any unmatched call fails the
    test, so a page the code must not fetch is asserted by omission."""
    def fake_run(argv, timeout=None):
        joined = " ".join(argv)
        for key, stdout in payloads.items():
            if key in joined:
                return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        raise AssertionError(f"unexpected gh call: {joined}")
    return fake_run


def test_bot_closes_filters_to_the_bot_actor(monkeypatch):
    """Candidates come from the closed-PR listing (newest-updated first) —
    merged PRs and closes outside the window drop out, and paging stops once a
    page's updates predate the window, so page 2 is never fetched. Only PRs
    whose latest upstream close event was performed by the configured bot come
    back, each carrying that event's own timestamp; GitHub reports an app actor
    under its "[bot]"-suffixed login, so both that form and the bare configured
    login must match."""
    page1 = json.dumps([
        {"number": 11, "closed_at": "2026-07-27T18:59:00Z", "merged_at": None,
         "updated_at": "2026-07-27T19:30:00Z"},
        {"number": 12, "closed_at": "2026-07-27T19:00:00Z", "merged_at": None,
         "updated_at": "2026-07-27T19:20:00Z"},
        {"number": 13, "closed_at": "2026-07-27T19:01:00Z", "merged_at": None,
         "updated_at": "2026-07-27T19:05:00Z"},
        {"number": 14, "closed_at": "2026-07-27T19:01:00Z",  # merged → not a close
         "merged_at": "2026-07-27T19:01:00Z", "updated_at": "2026-07-27T19:01:00Z"},
        {"number": 15, "closed_at": "2026-07-27T17:00:00Z",  # outside the window
         "merged_at": None, "updated_at": "2026-07-27T18:40:00Z"},  # predates it → stop
    ])
    monkeypatch.setattr(ab, "run", _gh({
        "page=1": page1,
        "/issues/11/events": json.dumps({"actor": f"{ab.BOT_LOGIN}[bot]", "at": "2026-07-27T18:59:26Z"}) + "\n",
        "/issues/12/events": json.dumps({"actor": "some-human", "at": "2026-07-27T19:00:00Z"}) + "\n",
        "/issues/13/events": json.dumps({"actor": ab.BOT_LOGIN, "at": "2026-07-27T19:01:00Z"}) + "\n",
    }))
    assert ab.bot_closes("2026-07-27T18:50:00Z", "2026-07-27T19:10:00Z") == [
        {"pr": 11, "closed_at": "2026-07-27T18:59:26Z"},
        {"pr": 13, "closed_at": "2026-07-27T19:01:00Z"},
    ]


def test_missing_skips_prs_with_a_landed_close(temp_store):
    activity.record("execute", identity="bot", dry_run=False, pr=11,
                    action="CLOSE_STALE", status="executed")
    closes = [{"pr": 11, "closed_at": "T1"}, {"pr": 12, "closed_at": "T2"}]
    assert ab.missing(closes) == [{"pr": 12, "closed_at": "T2"}]


def test_backfill_live_writes_backfilled_entries_and_heals_store_state(temp_store, tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    store.save_pr({"pr": 12, "meta": {"head_sha": "s", "checked_at": "t",
                                      "state": "open", "title": "x"}})
    monkeypatch.setattr(data, "store", lambda: store)

    entries = ab.backfill([{"pr": 12, "closed_at": "2026-07-27T19:01:22+00:00"}],
                          action="CLOSE_STALE", live=True)
    assert [e["pr"] for e in entries] == [12]
    [ev] = [e for e in activity.recent() if e.get("pr") == 12]
    assert ev["at"] == "2026-07-27T19:01:22+00:00"  # the upstream instant, not now
    assert ev["kind"] == "close" and ev["reason"] == "stale"
    assert ev["backfilled"] is True
    assert activity.run_state([12])[12]["done"] is True
    rec = store.load_pr(12)
    assert rec is not None and rec.state == "closed"


def test_backfill_dry_run_writes_nothing(temp_store, tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    store.save_pr({"pr": 12, "meta": {"head_sha": "s", "checked_at": "t",
                                      "state": "open", "title": "x"}})
    monkeypatch.setattr(data, "store", lambda: store)

    entries = ab.backfill([{"pr": 12, "closed_at": "T"}], action="CLOSE_STALE", live=False)
    assert len(entries) == 1
    assert activity.recent() == []
    rec = store.load_pr(12)
    assert rec is not None and rec.state == "open"


def test_main_dry_run_reports_without_writing(temp_store, monkeypatch, capsys):
    monkeypatch.setattr(ab, "bot_closes",
                        lambda since, until: [{"pr": 5, "closed_at": "T"}])
    rc = ab.main(["--since", "A", "--until", "B", "--action", "CLOSE_STALE"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["dry_run"] is True and out["bot_closes"] == 1
    assert [e["pr"] for e in out["entries"]] == [5]
    assert activity.recent() == []
