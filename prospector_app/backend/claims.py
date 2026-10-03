"""Operator item claims — who is working which PR or issue (#323).

One shared registry row (``claims``) in the store database, so every
operator's app shows the same markers: ``{items: {"pr:123": {by, machine,
at}}}``. A claim is advisory — it marks the item on Home rows and in the
flyouts, and the action bars warn before an operator acts on an item someone
else claimed; it never blocks the executor.
"""
from __future__ import annotations

import logging

from pipeline import settings
from pipeline import storekit
from prospector_app.backend import activity
from prospector_app.backend import data
from prospector_app.backend.snapshot import LazySnapshot

_log = logging.getLogger(__name__)

KINDS = ("pr", "issue")

_TTL_SEC = 5.0
_items: dict[str, dict] = {}


def key(kind: str, n: int) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown claim kind {kind!r}")
    return f"{kind}:{int(n)}"


def me() -> dict[str, str]:
    """The claiming identity: the operator's display name plus this machine."""
    return {"by": activity.operator()["name"], "machine": settings.worker_id()}


def _freshen(full: bool) -> None:
    """Read every live claim. Fails soft: an unreadable store reads as no
    claims on the first read, and keeps the last claims after that."""
    global _items
    try:
        _items = (data.store().load_claims() or {}).get("items") or {}
    except Exception:
        _log.warning("claims.load: store read failed", exc_info=True)
        if full:
            _items = {}


# The store is a network round-trip and a list request reads one claim per row,
# so the claims are held in memory: read on the first call, then re-read in the
# background at most every _TTL_SEC, which bounds how long another operator's
# claim takes to appear. This operator's own claim or release reads on the next
# call.
_snapshot = LazySnapshot(_freshen, debounce=_TTL_SEC)


def load() -> dict[str, dict]:
    """Every live claim, ``{key: {by, machine, at}}``."""
    _snapshot.ensure()
    return _items


def for_item(kind: str, n: int) -> dict | None:
    return load().get(key(kind, n))


def claim(kind: str, n: int) -> dict:
    """Mark the item as being worked by this operator on this machine.
    Returns the record stored."""
    rec = {**me(), "at": storekit.now()}
    st = data.store()
    reg = st.load_claims()
    reg.setdefault("items", {})[key(kind, n)] = rec
    st.save_claims(reg)
    _snapshot.invalidate()
    return rec


def release(kind: str, n: int) -> None:
    """Drop the item's claim, whoever holds it."""
    st = data.store()
    reg = st.load_claims()
    items = reg.setdefault("items", {})
    if items.pop(key(kind, n), None) is not None:
        st.save_claims(reg)
    _snapshot.invalidate()
