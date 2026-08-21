"""Cached read-side access for the Advisories sub-view: one light snapshot over
the advisory store. Nothing here runs at app startup."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from alert_triage.advisory_store import AdvisoryStore
from prospector_app.backend.snapshot import LazySnapshot

if TYPE_CHECKING:
    from alert_triage.advisory_model import Advisory

STORE_ROOT: Path | None = None
CHECK_DEBOUNCE = 10.0


@dataclass
class _State:
    store: AdvisoryStore | None = None
    advisories: dict[int, Advisory] = field(default_factory=dict)
    watermark: str | None = None

    def reset(self) -> None:
        self.store = None
        self.advisories = {}
        self.watermark = None


_state = _State()


def set_store_root(root: Path | str | None) -> None:
    global STORE_ROOT
    normalized = Path(root) if root is not None else None
    if normalized == STORE_ROOT:
        return
    STORE_ROOT = normalized
    _state.reset()
    _snapshot.invalidate()


def store() -> AdvisoryStore:
    if _state.store is None:
        _state.store = AdvisoryStore(STORE_ROOT)
    return _state.store


def _freshen(full: bool = False) -> None:
    delta, hi = store().advisories_since(None if full else _state.watermark)
    _state.advisories = dict(delta) if full else {**_state.advisories, **delta}
    if hi:
        _state.watermark = (hi if (full or _state.watermark is None)
                            else max(_state.watermark, hi))


_snapshot = LazySnapshot(_freshen, debounce=CHECK_DEBOUNCE)


def advisories() -> dict[int, Advisory]:
    _snapshot.ensure()
    return _state.advisories


def refresh() -> None:
    _snapshot.refresh()
