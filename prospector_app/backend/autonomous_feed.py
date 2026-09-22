"""The Home "Done on its own" feed — landed upstream actions no person approved.

Selects the activity-log events stamped ``initiator="worker"``: a push the
automation took on its own judgment (an autopushed hunt action, a
machine-approved resolve) or a comment it posted itself (a reviewer
re-trigger). Operator clicks and operator-approved parked changes carry no
such stamp and stay out. Each item names the undo the app can offer — a
reopen for a close — or None where none exists, so the feed is reviewable
after the fact rather than an approval queue.
"""
from __future__ import annotations

from prospector_app.backend import activity

# canonical kind -> the undo the app can perform on it.
UNDO: dict[str, str] = {"close": "reopen-pr", "issue-close": "reopen-issue"}


def item(ev: dict) -> dict:
    """One feed row: the event's who/what/when plus its available undo."""
    kind = activity.canonical_kind(ev)
    return {
        "at": ev.get("at"),
        "kind": kind,
        "action": ev.get("action"),
        "pr": ev.get("pr"),
        "issue": ev.get("issue"),
        "identity": ev.get("identity"),
        "detail": ev.get("detail") or ev.get("message"),
        "undo": UNDO.get(kind),
    }


def feed(limit: int = 50, events: list[dict] | None = None) -> list[dict]:
    """The newest ``limit`` landed worker-initiated actions, newest first.
    Dry-runs, errors, and every event without the worker stamp are excluded."""
    events = activity.all_events() if events is None else events
    out: list[dict] = []
    for ev in events:
        if ev.get("initiator") != "worker" or not activity.is_landed(ev):
            continue
        out.append(item(ev))
        if len(out) >= limit:
            break
    return out
