"""Action run-state (#10): the latest landed live action per PR, from the DB,
so the UI can mark a PR done and guard a re-fire. Survives refresh (DB-backed)."""
from __future__ import annotations

from prospector_app.backend import activity


def _insert(url: str, events: list[dict]) -> None:
    """Insert raw activity events into the temp DB, bypassing normalize() so
    legacy shapes are preserved exactly as stored."""
    from pipeline import schema
    from pipeline import storekit
    eng = storekit.get_engine(url)
    with eng.begin() as conn:
        for e in events:
            conn.execute(schema.activity.insert().values(**schema.activity_row(e)))


def test_executed_close_is_done_and_undoable(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-10T10:00:00", "kind": "close", "reason": "duplicate",
         "pr": 66, "dry_run": False, "status": "executed", "action": "CLOSE_DUP"},
    ])
    st = activity.run_state([66])
    assert st[66]["done"] is True and st[66]["undoable"] is True
    assert st[66]["reason"] == "duplicate" and st[66]["at"] == "2026-06-10T10:00:00"


def test_dry_run_is_not_counted(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-10T10:00:00", "kind": "close", "pr": 66, "dry_run": True, "status": "dry-run"},
    ])
    assert activity.run_state([66]) == {}


def test_reopen_after_close_is_actionable_again(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-10T10:00:00", "kind": "close", "pr": 66, "dry_run": False, "status": "executed"},
        {"at": "2026-06-10T10:05:00", "kind": "reopen", "pr": 66, "dry_run": False, "status": "reopened"},
    ])
    st = activity.run_state([66])
    assert st[66]["kind"] == "reopen" and st[66]["done"] is False


def test_merge_is_done_but_not_undoable(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-10T10:00:00", "kind": "merge", "pr": 70, "dry_run": False, "status": "merged"},
    ])
    st = activity.run_state([70])
    assert st[70]["done"] is True and st[70]["undoable"] is False


def test_errors_are_ignored(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-10T10:00:00", "kind": "close", "pr": 66, "dry_run": False, "status": "error"},
    ])
    assert activity.run_state([66]) == {}


def test_only_requested_prs_returned(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-10T10:00:00", "kind": "close", "pr": 66, "dry_run": False, "status": "executed"},
        {"at": "2026-06-10T10:01:00", "kind": "close", "pr": 99, "dry_run": False, "status": "executed"},
    ])
    assert set(activity.run_state([66])) == {66}


def test_legacy_execute_row_normalizes(temp_store):
    # Pre-#40 row: mechanical kind="execute" + action; run_state should still
    # see it as a landed close via normalize().
    _insert(temp_store, [
        {"at": "2026-06-09T10:30:11", "kind": "execute", "action": "CLOSE_STALE",
         "pr": 66, "dry_run": False, "status": "executed"},
    ])
    st = activity.run_state([66])
    assert st[66]["kind"] == "close" and st[66]["done"] is True and st[66]["reason"] == "stale"
