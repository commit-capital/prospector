"""activity.for_pr: the per-PR record of what really happened to a PR — landed
actions only (no dry-runs, no failed/skipped attempts), newest-first, each with
the bot identity and the human operator."""
from __future__ import annotations

from review_cockpit.backend import activity


def _insert(url: str, rows: list[dict]) -> None:
    """Insert raw activity events into the temp DB."""
    from pipeline import schema
    from pipeline import storekit
    eng = storekit.get_engine(url)
    with eng.begin() as conn:
        for r in rows:
            conn.execute(schema.activity.insert().values(**schema.activity_row(r)))


def test_for_pr_returns_landed_actions_newest_first(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-17T10:00:00", "kind": "close", "action": "CLOSE", "status": "executed",
         "identity": "test-bot", "operator": "Alex Example", "pr": 6418, "reason": "manual",
         "detail": "commented + closed", "dry_run": False},
        {"at": "2026-06-17T11:00:00", "kind": "reopen", "status": "reopened",
         "identity": "test-bot", "operator": "Jordan Example", "pr": 6418, "dry_run": False,
         "event_url": "https://github.com/o/r/pull/6418#issuecomment-2"},
        {"at": "2026-06-17T09:00:00", "kind": "close", "status": "executed",
         "identity": "test-bot", "operator": "X", "pr": 999, "dry_run": False},   # other PR
        {"at": "2026-06-17T08:00:00", "kind": "close", "status": "dry-run",
         "identity": "test-bot", "operator": "X", "pr": 6418, "dry_run": True},   # preview
        {"at": "2026-06-17T07:00:00", "kind": "close", "status": "error",
         "identity": "test-bot", "operator": "X", "pr": 6418, "dry_run": False},  # failed
    ])
    items = activity.for_pr(6418)
    assert [i["kind"] for i in items] == ["reopen", "close"]   # newest first; only the 2 landed
    assert items[1]["operator"] == "Alex Example"
    assert items[1]["identity"] == "test-bot"
    assert items[1]["detail"] == "commented + closed"
    assert items[0]["event_url"] == "https://github.com/o/r/pull/6418#issuecomment-2"
    assert items[1]["event_url"] is None  # no event captured → no deep-link


def test_for_pr_empty_when_no_actions(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-17T10:00:00", "kind": "close", "status": "executed",
         "identity": "test-bot", "operator": "X", "pr": 123, "dry_run": False},
    ])
    assert activity.for_pr(6418) == []
