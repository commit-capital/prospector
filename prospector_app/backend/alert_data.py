"""Cached read-side access for the Alerts tab.

One light snapshot over the alert store — alerts are a small corpus, so there
is no omit/hydrate split. Nothing here runs at app startup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from alert_triage.alert_store import AlertStore
from pipeline import storekit
from prospector_app.backend.snapshot import LazySnapshot

if TYPE_CHECKING:
    from alert_triage.alert_model import Alert

STORE_ROOT: Path | None = None
CHECK_DEBOUNCE = 10.0


@dataclass
class _AlertSnapshotState:
    store: AlertStore | None = None
    alerts: dict[int, Alert] = field(default_factory=dict)
    watermark: str | None = None
    runs: list[storekit.RunRecord] = field(default_factory=list)

    def reset(self) -> None:
        self.store = None
        self.alerts = {}
        self.watermark = None
        self.runs = []


_state = _AlertSnapshotState()


def set_store_root(root: Path | str | None) -> None:
    global STORE_ROOT
    normalized = Path(root) if root is not None else None
    if normalized == STORE_ROOT:
        return
    STORE_ROOT = normalized
    _state.reset()
    _snapshot.invalidate()
    _runs_snapshot.invalidate()


def store() -> AlertStore:
    if _state.store is None:
        _state.store = AlertStore(STORE_ROOT)
    return _state.store


def _freshen(full: bool = False) -> None:
    delta, hi = store().alerts_since(None if full else _state.watermark)
    _state.alerts = dict(delta) if full else {**_state.alerts, **delta}
    if hi:
        _state.watermark = (hi if (full or _state.watermark is None)
                            else max(_state.watermark, hi))


_snapshot = LazySnapshot(_freshen, debounce=CHECK_DEBOUNCE)


def _freshen_runs(full: bool) -> None:
    """Re-read the whole alert runs ledger (it has no watermark API, so every
    freshen is a full read and `full` is irrelevant)."""
    _state.runs = store().runs()


_runs_snapshot = LazySnapshot(_freshen_runs, debounce=CHECK_DEBOUNCE)


def alerts() -> dict[int, Alert]:
    _snapshot.ensure()
    return _state.alerts


def runs() -> list[storekit.RunRecord]:
    """The alert pipeline's runs ledger, typed, oldest first — served from a
    debounced snapshot."""
    _runs_snapshot.ensure()
    return _state.runs


def refresh() -> None:
    _snapshot.refresh()
