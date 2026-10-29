"""The verify worker's review slots: security and design picks run beside the
drain loop's sandbox runs, never two on one PR, and stop with the worker."""
from __future__ import annotations

import threading

import pytest

from pipeline import store as S
from prospector_app.backend import data, verify_worker
from prospector_app.backend.tests.test_autohunt import _clean_merge_pr

WAIT = 10.0


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """A worker whose sandbox, agents and heartbeat are stubbed, with a fresh
    stop event and in-flight set; stopped after the test."""
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_WORKER_ID", "mac")
    monkeypatch.setenv("TRIAGE_VERIFY_WORKER", "1")
    monkeypatch.setenv("TRIAGE_VERIFY_AUTOHUNT", "1")
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "off")
    monkeypatch.setattr(verify_worker, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(verify_worker, "stop", threading.Event())
    monkeypatch.setattr(verify_worker, "_recovered", threading.Event())
    monkeypatch.setattr(verify_worker, "_reviewing", set())
    monkeypatch.setattr(verify_worker, "_threads", [])
    monkeypatch.setattr(verify_worker, "_last_auto_lane", None)
    monkeypatch.setattr(verify_worker, "_changed_paths", lambda pr: ["src/app.ts"])
    monkeypatch.setattr(verify_worker, "_recover", lambda: None)
    monkeypatch.setattr(verify_worker, "beat", lambda: None)
    monkeypatch.setattr(verify_worker, "_clear_heartbeat", lambda: None)
    monkeypatch.setattr(verify_worker, "maybe_refresh_base", lambda: None)
    monkeypatch.setattr(verify_worker, "hold_stale_pin", lambda: None)
    monkeypatch.setattr(verify_worker, "_note_verify_ending", lambda n: None)
    monkeypatch.setattr(verify_worker.worker_log, "install", lambda: None)
    monkeypatch.setattr(verify_worker.lane_health, "open_or_retest", lambda lane: True)
    monkeypatch.setattr(verify_worker.lane_health, "is_open", lambda lane: True)
    monkeypatch.setattr(verify_worker.lane_health, "capacity_open", lambda lane: True)
    verify_worker.security_failed.clear()
    yield st
    verify_worker.shutdown(timeout=WAIT)
    verify_worker.security_failed.clear()


def test_a_running_verification_does_not_hold_a_security_review(worker, monkeypatch):
    worker.save_pr(_clean_merge_pr(1))
    worker.save_pr(_clean_merge_pr(2))
    worker.edit_pr(2).record_verify_request("queued", queued_at="2026-07-01T00:00:00+00:00")
    data.refresh()
    verifying, release, reviewed = threading.Event(), threading.Event(), threading.Event()

    def run_one(n: int) -> int:
        verifying.set()
        release.wait(WAIT)
        worker.edit_pr(n).record_verify_request("done", queued_at="2026-07-01T00:00:00+00:00")
        data.refresh()
        return 0

    def run_security(n: int) -> int:
        assert verifying.is_set() and not release.is_set()
        reviewed.set()
        verify_worker.stop.wait(WAIT)
        return 0
    monkeypatch.setattr(verify_worker, "run_one", run_one)
    monkeypatch.setattr(verify_worker, "run_security", run_security)
    assert verify_worker.startup()
    try:
        assert verifying.wait(WAIT)
        assert reviewed.wait(WAIT)
        assert verify_worker.reviewing() == [1]
    finally:
        release.set()


def test_two_slots_never_review_one_pr(worker, monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_SLOTS", "3")
    for n in (1, 2):
        worker.save_pr(_clean_merge_pr(n))
    data.refresh()
    picked: list[int] = []
    lock = threading.Lock()
    both = threading.Barrier(3, timeout=WAIT)

    def run_security(n: int) -> int:
        with lock:
            picked.append(n)
        both.wait()
        verify_worker.stop.wait(WAIT)
        return 0
    monkeypatch.setattr(verify_worker, "run_one", lambda n: 0)
    monkeypatch.setattr(verify_worker, "run_security", run_security)
    assert verify_worker.startup()
    verify_worker._recovered.wait(WAIT)
    both.wait()
    # The third slot has had many polls to take a PR; there is none left.
    verify_worker.stop.wait(0.2)
    assert sorted(picked) == [1, 2]
    assert verify_worker.reviewing() == [1, 2]


def test_no_slot_picks_before_the_startup_cleanup(worker, monkeypatch):
    worker.save_pr(_clean_merge_pr(1))
    data.refresh()
    monkeypatch.setattr(verify_worker, "run_security",
                        lambda n: pytest.fail("picked before the cleanup"))
    monkeypatch.setattr(verify_worker, "stop", _Ticks(5))
    verify_worker._review_loop()


def test_shutdown_stops_the_drain_loop_and_every_slot(worker, monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_SLOTS", "2")
    monkeypatch.setattr(verify_worker, "run_one", lambda n: 0)
    assert verify_worker.startup()
    names = sorted(t.name for t in verify_worker._threads)
    assert names == ["verify-worker", "verify-worker-beat",
                     "verify-worker-review-0", "verify-worker-review-1"]
    threads = list(verify_worker._threads)
    assert verify_worker.shutdown(timeout=WAIT)
    assert not any(t.is_alive() for t in threads)
    assert not verify_worker.running()


class _Ticks:
    """A stop event that ends the loop after `n` checks and never waits."""

    def __init__(self, n: int) -> None:
        self.left = n

    def is_set(self) -> bool:
        self.left -= 1
        return self.left < 0

    def wait(self, seconds: float) -> bool:
        return False
