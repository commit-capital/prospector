"""Activity-log semantic kinds (#40): new events get a canonical top-level
kind; legacy mechanical rows are normalized on read with no migration."""
from __future__ import annotations

from datetime import datetime, timedelta

from prospector_app.backend import activity


def test_close_actions_map_to_close_with_reason():
    for action, reason in [("CLOSE_DUP", "duplicate"), ("CLOSE_FIXED", "already-fixed"),
                           ("CLOSE_STALE", "stale"), ("CLOSE_OVERSIZED", "oversized"),
                           ("CLOSE", "manual")]:
        ev = {"kind": "execute", "action": action}
        assert activity.canonical_kind(ev) == "close"
        assert activity.close_reason(ev) == reason


def test_legacy_mechanical_kinds_normalize():
    assert activity.canonical_kind({"kind": "execute", "action": "CLOSE_DUP"}) == "close"
    assert activity.canonical_kind({"kind": "review", "action": "REVIEW:REQUEST_CHANGES"}) == "comment"
    assert activity.canonical_kind({"kind": "line_comment", "action": "LINE_COMMENT"}) == "comment"
    assert activity.canonical_kind({"kind": "reopen", "action": "REOPEN"}) == "reopen"
    assert activity.canonical_kind({"kind": "merge", "action": "MERGE"}) == "merge"
    assert activity.canonical_kind({"kind": "emit", "cluster": 1}) == "handoff"


def test_canonical_kinds_pass_through():
    for k in activity.KINDS:
        assert activity.canonical_kind({"kind": k}) == k


def test_normalize_adds_reason_for_close():
    out = activity.normalize({"kind": "execute", "action": "CLOSE_STALE"})
    assert out["kind"] == "close" and out["reason"] == "stale"


def test_normalize_is_pure():
    src = {"kind": "execute", "action": "CLOSE_DUP"}
    activity.normalize(src)
    assert src["kind"] == "execute"  # original untouched


def _fixed_operator(monkeypatch, *, slug: str = "alice", name: str = "Alice Example") -> None:
    """Monkeypatch activity.operator() to a fixed identity so record() attributes
    events to a known name/slug regardless of the machine's git config."""
    monkeypatch.setattr(activity, "operator",
                        lambda: {"name": name, "email": f"{slug}@example.com", "slug": slug})


def _insert(url: str, events: list[dict]) -> None:
    """Insert raw activity events into the temp DB, bypassing normalize() so
    legacy shapes are preserved exactly as stored."""
    from pipeline import schema
    from pipeline import storekit
    eng = storekit.get_engine(url)
    with eng.begin() as conn:
        for e in events:
            conn.execute(schema.activity.insert().values(**schema.activity_row(e)))


def test_record_writes_semantic_kind(temp_store, monkeypatch):
    _fixed_operator(monkeypatch, slug="alice", name="Alice Example")
    ev = activity.record("execute", identity="test-bot", dry_run=False, pr=66,
                         action="CLOSE_DUP", status="executed", detail="commented + closed")
    # record() normalizes on write; return value and the DB row carry the semantic kind.
    assert ev["kind"] == "close" and ev["reason"] == "duplicate"
    assert ev["action"] == "CLOSE_DUP"          # mechanical detail preserved
    assert ev["operator"] == "Alice Example"    # human attribution stamped
    # Reader also returns the normalized row.
    written = activity.recent()[0]
    assert written["kind"] == "close" and written["reason"] == "duplicate"


def test_dry_run_is_visible_in_recent(temp_store, monkeypatch):
    _fixed_operator(monkeypatch, slug="alice")
    activity.record("execute", dry_run=True, pr=66, action="CLOSE", status="dry-run")
    assert activity.recent()[0]["pr"] == 66     # visible in the feed
    assert activity.run_state([66]) == {}       # not counted as a landed action


def test_recent_merges_events_across_operators_chronologically(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-10T09:00:00+00:00", "kind": "close", "pr": 1, "operator": "Alice",
         "dry_run": False, "status": "executed"},
        {"at": "2026-06-10T11:00:00+00:00", "kind": "merge", "pr": 2, "operator": "Bob",
         "dry_run": False, "status": "merged"},
    ])
    prs = [ev["pr"] for ev in activity.recent()]
    assert prs == [2, 1]  # newest-first across both operators' events


def test_record_stamps_timezone_aware_utc(temp_store, monkeypatch):
    """The `at` stamp is a timezone-aware UTC instant, not a naive local
    wall-clock time. Operators sit in different timezones, so a naive stamp makes
    one teammate's events sort against another's by mismatched clocks."""
    _fixed_operator(monkeypatch, slug="alice")
    ev = activity.record("execute", dry_run=False, pr=1, action="CLOSE", status="executed")
    stamped = datetime.fromisoformat(ev["at"])
    assert stamped.tzinfo is not None              # carries an offset, not naive
    assert stamped.utcoffset() == timedelta(0)     # and that offset is UTC


def test_recent_orders_by_true_instant_across_timezones(temp_store):
    """Events order by the true instant, not by each machine's wall-clock string.
    Casey (UTC+2) reopened at 10:00Z — his clock read 12:00 — then Taylor (UTC+0)
    merged two hours later at 12:00Z. The merge is newer and must sort first even
    though Casey's wall-clock string ('12:00') reads later than Taylor's."""
    _insert(temp_store, [
        {"at": "2026-06-23T12:00:00+02:00", "kind": "reopen", "pr": 1, "operator": "Casey",
         "dry_run": False, "status": "reopened"},
        {"at": "2026-06-23T12:00:00+00:00", "kind": "merge", "pr": 2, "operator": "Taylor",
         "dry_run": False, "status": "merged"},
    ])
    prs = [ev["pr"] for ev in activity.recent()]
    assert prs == [2, 1]  # merge (12:00Z) newest; reopen (10:00Z) older


def test_recent_normalizes_legacy_archive_rows(temp_store):
    _insert(temp_store, [
        {"at": "2026-06-09T10:30:11", "kind": "execute",
         "action": "CLOSE_STALE", "pr": 66, "status": "dry-run"},
    ])
    [ev] = activity.recent()
    assert ev["kind"] == "close" and ev["reason"] == "stale"


def test_slug_is_filesystem_safe_and_attributable():
    assert activity._slug("Alex Example") == "alex-example"
    assert activity._slug("a..b/@c") == "a-b-c"
    assert activity._slug("") == "unknown"
