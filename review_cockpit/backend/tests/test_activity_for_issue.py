"""activity.for_issue: the per-issue record of what really happened to an issue —
landed actions only (no dry-runs, no failed/blocked attempts), newest-first, each
with the bot identity, the human operator, and the issue-close specifics
(reason, canonical for dups, fixing PR for fixed)."""
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


def test_for_issue_returns_landed_actions_newest_first(temp_store):
    _insert(temp_store, [
        {"at": "2026-07-17T10:00:00", "kind": "issue-close", "action": "CLOSE_ISSUE",
         "status": "closed", "reason": "not planned", "identity": "test-bot",
         "operator": "Alex Example", "issue": 2928, "dry_run": False,
         "detail": "commented + closed"},
        {"at": "2026-07-17T11:00:00", "kind": "issue-reopen", "status": "reopened",
         "identity": "test-bot", "operator": "Jordan Example", "issue": 2928,
         "dry_run": False},
        {"at": "2026-07-17T09:00:00", "kind": "issue-close", "status": "closed",
         "identity": "test-bot", "operator": "X", "issue": 999,
         "dry_run": False},                                        # other issue
        {"at": "2026-07-17T08:00:00", "kind": "issue-close", "status": "dry-run",
         "identity": "test-bot", "operator": "X", "issue": 2928,
         "dry_run": True},                                         # preview
        {"at": "2026-07-17T07:00:00", "kind": "issue-close", "status": "blocked",
         "identity": "test-bot", "operator": "X", "issue": 2928,
         "dry_run": False},                                        # gate-blocked
        {"at": "2026-07-17T06:00:00", "kind": "close", "status": "executed",
         "identity": "test-bot", "operator": "X", "pr": 2928,
         "dry_run": False},                                        # PR, same number
    ])
    items = activity.for_issue(2928)
    assert [i["kind"] for i in items] == ["issue-reopen", "issue-close"]
    assert items[1]["operator"] == "Alex Example"
    assert items[1]["identity"] == "test-bot"
    assert items[1]["reason"] == "not planned"
    assert items[1]["detail"] == "commented + closed"


def test_for_issue_carries_dup_and_fixed_specifics(temp_store):
    _insert(temp_store, [
        {"at": "2026-07-17T10:00:00", "kind": "issue-close", "action": "CLOSE_ISSUE_DUP",
         "status": "closed", "identity": "test-bot", "operator": "X",
         "issue": 100, "canonical": 42, "dry_run": False},
        {"at": "2026-07-17T11:00:00", "kind": "issue-close", "action": "CLOSE_ISSUE_FIXED",
         "status": "closed", "identity": "test-bot", "operator": "X",
         "issue": 101, "fixed_by": 7, "via": "merge", "dry_run": False},
    ])
    (dup,) = activity.for_issue(100)
    assert dup["action"] == "CLOSE_ISSUE_DUP" and dup["canonical"] == 42
    (fixed,) = activity.for_issue(101)
    assert fixed["action"] == "CLOSE_ISSUE_FIXED"
    assert fixed["fixed_by"] == 7 and fixed["via"] == "merge"


def test_for_issue_empty_when_no_actions(temp_store):
    _insert(temp_store, [
        {"at": "2026-07-17T10:00:00", "kind": "issue-close", "status": "closed",
         "identity": "test-bot", "operator": "X", "issue": 123, "dry_run": False},
    ])
    assert activity.for_issue(2928) == []
