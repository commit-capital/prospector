from __future__ import annotations

from prospector_app.backend.snapshot import LazySnapshot


def test_a_run_of_writes_costs_one_freshen_on_the_next_read():
    calls: list[bool] = []
    snap = LazySnapshot(calls.append, debounce=3600)
    snap.ensure()
    for _ in range(23):
        snap.mark_stale()
    assert calls == [True]
    snap.ensure()
    snap.ensure()
    assert calls == [True, False]


def test_a_failed_freshen_serves_the_last_snapshot_and_retries():
    calls: list[bool] = []

    def freshen(full: bool) -> None:
        calls.append(full)
        if len(calls) == 2:
            raise RuntimeError("store down")

    snap = LazySnapshot(freshen, debounce=3600)
    snap.ensure()
    snap.mark_stale()
    snap.ensure()
    snap.ensure()
    assert calls == [True, False, False]


def test_a_refresh_clears_a_pending_stale_mark():
    calls: list[bool] = []
    snap = LazySnapshot(calls.append, debounce=3600)
    snap.ensure()
    snap.mark_stale()
    snap.refresh()
    snap.ensure()
    assert calls == [True, False]
