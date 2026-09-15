"""Each worker sweeps the sandbox containers a previous process left running
before its loop can launch a phase beside them."""
from __future__ import annotations

import threading

import pytest

from pipeline import verify_driver
from prospector_app.backend import fix_worker
from prospector_app.backend import verify_worker


@pytest.fixture
def stopped(monkeypatch):
    """A stop event already set, so each loop runs its startup and exits."""
    ev = threading.Event()
    ev.set()
    monkeypatch.setattr(verify_worker, "stop", ev)
    monkeypatch.setattr(fix_worker, "stop", ev)
    return ev


def _swept(monkeypatch) -> list[str]:
    swept: list[str] = []
    monkeypatch.setattr(verify_driver, "stop_orphaned_sandboxes",
                        lambda: swept.append("swept") or [])
    return swept


def test_verify_worker_stops_orphaned_sandboxes_before_recovering(monkeypatch, stopped):
    order = _swept(monkeypatch)
    monkeypatch.setattr(verify_worker, "release_stale_claims",
                        lambda: order.append("claims") or [])
    monkeypatch.setattr(verify_worker, "recover_orphans",
                        lambda: order.append("orphans") or ([], []))
    verify_worker._drain_loop()
    assert order == ["swept", "claims", "orphans"]


def test_fix_worker_stops_orphaned_sandboxes_before_recovering(monkeypatch, stopped):
    order = _swept(monkeypatch)
    monkeypatch.setattr(fix_worker, "recover_orphans", lambda: order.append("orphans") or [])
    fix_worker._drain_loop()
    assert order == ["swept", "orphans"]


def test_a_sweep_that_raises_does_not_stop_the_worker(monkeypatch, stopped):
    def boom() -> list[str]:
        raise RuntimeError("docker exploded")
    monkeypatch.setattr(verify_driver, "stop_orphaned_sandboxes", boom)
    recovered: list[bool] = []
    monkeypatch.setattr(verify_worker, "release_stale_claims", lambda: [])
    monkeypatch.setattr(verify_worker, "recover_orphans",
                        lambda: recovered.append(True) or ([], []))
    verify_worker._drain_loop()
    assert recovered == [True]
