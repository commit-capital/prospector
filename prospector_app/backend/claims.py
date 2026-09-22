"""Operator item claims — who is working which PR or issue (#323).

One shared registry row (``claims``) in the store database, so every
operator's app shows the same markers: ``{items: {"pr:123": {by, machine,
at}}}``. A claim is advisory — it marks the item on Home rows and in the
flyouts, and the action bars warn before an operator acts on an item someone
else claimed; it never blocks the executor.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from pipeline import settings
from prospector_app.backend import activity
from prospector_app.backend import data

_log = logging.getLogger(__name__)

KINDS = ("pr", "issue")

_TTL_SEC = 5.0
_cache: tuple[float, dict[str, dict]] | None = None


def key(kind: str, n: int) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown claim kind {kind!r}")
    return f"{kind}:{int(n)}"


def me() -> dict[str, str]:
    """The claiming identity: the operator's display name plus this machine."""
    return {"by": activity.operator()["name"], "machine": settings.worker_id()}


def load() -> dict[str, dict]:
    """Every live claim, ``{key: {by, machine, at}}``. Cached for _TTL_SEC
    because ``pr_row`` runs once per row of a list request while the store is
    a network round-trip; the TTL bounds how long another operator's claim
    takes to appear. Fails soft — an unreadable store shows every item
    unclaimed rather than failing the request."""
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _TTL_SEC:
        return _cache[1]
    try:
        items = (data.store().load_claims() or {}).get("items") or {}
    except Exception:
        _log.warning("claims.load: store read failed", exc_info=True)
        items = {}
    _cache = (now, items)
    return items


def for_item(kind: str, n: int) -> dict | None:
    return load().get(key(kind, n))


def claim(kind: str, n: int) -> dict:
    """Mark the item as being worked by this operator on this machine.
    Returns the record stored."""
    global _cache
    rec = {**me(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    st = data.store()
    reg = st.load_claims()
    reg.setdefault("items", {})[key(kind, n)] = rec
    st.save_claims(reg)
    _cache = None
    return rec


def release(kind: str, n: int) -> None:
    """Drop the item's claim, whoever holds it."""
    global _cache
    st = data.store()
    reg = st.load_claims()
    items = reg.setdefault("items", {})
    if items.pop(key(kind, n), None) is not None:
        st.save_claims(reg)
    _cache = None
