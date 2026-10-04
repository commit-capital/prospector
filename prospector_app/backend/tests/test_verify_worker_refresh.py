"""The daily verify-base pin refresh: due-ness is pure; the attempt stamps
once per day, keeps the old pin on failure, and ledgers every attempt; a pin
past its age limit holds the verify lane until it is current again."""
from datetime import datetime, timezone

import pytest

from pipeline import settings, worker_health
from pipeline import store as S
from pipeline.storekit import now as _now
from prospector_app.backend import lane_health, verify_worker, worker_selftest


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


class TestBaseRefreshDue:
    def test_due_when_pin_is_older_than_a_day(self):
        reg = {"base_sha": "a" * 40, "pinned_at": "2026-07-20T12:00:00+00:00"}
        assert verify_worker.base_refresh_due(reg, NOW) is True

    def test_not_due_when_pin_is_fresh(self):
        reg = {"base_sha": "a" * 40, "pinned_at": "2026-07-22T02:00:00+00:00"}
        assert verify_worker.base_refresh_due(reg, NOW) is False

    def test_not_due_without_a_pin(self):
        assert verify_worker.base_refresh_due({}, NOW) is False

    def test_one_attempt_per_day(self):
        reg = {"base_sha": "a" * 40, "pinned_at": "2026-07-20T12:00:00+00:00",
               "refresh_attempted_at": "2026-07-22T01:00:00+00:00"}
        assert verify_worker.base_refresh_due(reg, NOW) is False

    def test_yesterdays_attempt_does_not_block(self):
        reg = {"base_sha": "a" * 40, "pinned_at": "2026-07-20T12:00:00+00:00",
               "refresh_attempted_at": "2026-07-21T23:00:00+00:00"}
        assert verify_worker.base_refresh_due(reg, NOW) is True


class FakeStore:
    """Holds one machine's pin — the refresh only ever reads and writes its
    own, so the host argument selects nothing here."""

    def __init__(self, reg):
        self.reg = reg
        self.runs = []

    def load_verify_base(self, host):
        return dict(self.reg)

    def save_verify_base(self, reg):
        self.reg = dict(reg)

    def append_run(self, entry):
        self.runs.append(entry)


class TestMaybeRefreshBase:
    def _wire(self, monkeypatch, reg):
        st = FakeStore(reg)
        monkeypatch.setattr(verify_worker.data, "store", lambda: st)
        return st

    def test_moved_head_triggers_prepare_base(self, monkeypatch):
        st = self._wire(monkeypatch, {
            "base_sha": "a" * 40, "tier": 1,
            "pinned_at": "2026-07-20T12:00:00+00:00"})
        monkeypatch.setattr(verify_worker.verify_driver, "resolve_base_sha",
                            lambda: "b" * 40)
        def mock_prepare_base(store, base_sha, tier):
            # Emulate real prepare_base: full-replace with all fields
            store.save_verify_base({
                "base_sha": base_sha,
                "tier": tier,
                "pinned_at": "2026-07-22T12:00:00+00:00",
                "baseline_failing": [],
                "baseline_captured_at": "2026-07-22T12:00:00+00:00",
                "suite": "test",
                "host": "mac-studio",
                "arch": "arm64"
            })
        monkeypatch.setattr(verify_worker.verify_driver, "prepare_base", mock_prepare_base)
        verify_worker.maybe_refresh_base()
        assert st.reg["refresh_attempted_at"]
        # Invariant: once-per-day stamp persists after prepare_base's full-replace
        assert verify_worker.base_refresh_due(st.reg, NOW) is False
        assert st.runs and st.runs[0]["phase"] == "verify:pin-refresh"
        assert st.runs[0]["stats"]["ok"] is True

    def test_unmoved_head_stamps_and_skips(self, monkeypatch):
        st = self._wire(monkeypatch, {
            "base_sha": "a" * 40, "tier": 1,
            "pinned_at": "2026-07-20T12:00:00+00:00"})
        monkeypatch.setattr(verify_worker.verify_driver, "resolve_base_sha",
                            lambda: "a" * 40)
        monkeypatch.setattr(verify_worker.verify_driver, "prepare_base",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("built")))
        verify_worker.maybe_refresh_base()
        assert st.reg["refresh_attempted_at"]
        assert st.runs[0]["stats"].get("unmoved") is True

    def test_failure_keeps_the_pin_and_ledgers(self, monkeypatch):
        st = self._wire(monkeypatch, {
            "base_sha": "a" * 40, "tier": 1,
            "pinned_at": "2026-07-20T12:00:00+00:00"})
        monkeypatch.setattr(verify_worker.verify_driver, "resolve_base_sha",
                            lambda: "b" * 40)
        def boom(*a, **k):
            raise RuntimeError("baseline capture failed")
        monkeypatch.setattr(verify_worker.verify_driver, "prepare_base", boom)
        verify_worker.maybe_refresh_base()
        assert st.reg["base_sha"] == "a" * 40
        assert st.runs[0]["stats"]["ok"] is False
        assert "baseline capture failed" in st.runs[0]["stats"]["error"]

    def test_a_failed_refresh_records_its_error_on_the_pin(self, monkeypatch):
        """The runs ledger is not a place anyone looks. The pin carries the
        outcome too, so the app can show a lane that has stopped tracking
        master."""
        st = self._wire(monkeypatch, {
            "base_sha": "a" * 40, "tier": 1,
            "pinned_at": "2026-07-20T12:00:00+00:00"})
        monkeypatch.setattr(verify_worker.verify_driver, "resolve_base_sha",
                            lambda: "b" * 40)

        def boom(*a, **k):
            raise RuntimeError("no space left on device")

        monkeypatch.setattr(verify_worker.verify_driver, "prepare_base", boom)
        verify_worker.maybe_refresh_base()
        assert st.reg["refresh_ok"] is False
        assert "no space left on device" in st.reg["refresh_error"]
        assert st.reg["refresh_failures"] == 1

    def test_consecutive_failures_accumulate(self, monkeypatch):
        st = self._wire(monkeypatch, {
            "base_sha": "a" * 40, "tier": 1, "refresh_failures": 2,
            "pinned_at": "2026-07-20T12:00:00+00:00"})
        monkeypatch.setattr(verify_worker.verify_driver, "resolve_base_sha",
                            lambda: "b" * 40)
        monkeypatch.setattr(verify_worker.verify_driver, "prepare_base",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        verify_worker.maybe_refresh_base()
        assert st.reg["refresh_failures"] == 3

    def test_a_successful_refresh_clears_the_failure_run(self, monkeypatch):
        """Written after prepare_base returns, because prepare_base
        full-replaces the pin."""
        st = self._wire(monkeypatch, {
            "base_sha": "a" * 40, "tier": 1, "refresh_failures": 4,
            "refresh_ok": False, "refresh_error": "no space left on device",
            "pinned_at": "2026-07-20T12:00:00+00:00"})
        monkeypatch.setattr(verify_worker.verify_driver, "resolve_base_sha",
                            lambda: "b" * 40)

        def mock_prepare_base(store, base_sha, tier):
            store.save_verify_base({
                "base_sha": base_sha, "tier": tier,
                "pinned_at": "2026-07-22T12:00:00+00:00", "baseline_failing": [],
                "baseline_captured_at": "2026-07-22T12:00:00+00:00"})

        monkeypatch.setattr(verify_worker.verify_driver, "prepare_base",
                            mock_prepare_base)
        verify_worker.maybe_refresh_base()
        assert st.reg["refresh_ok"] is True
        assert st.reg["refresh_failures"] == 0
        assert st.reg["refresh_error"] is None

    def test_an_unmoved_head_counts_as_a_healthy_refresh(self, monkeypatch):
        """Upstream simply has not moved. The lane is tracking master fine, so
        this must not read as a stalled refresh."""
        st = self._wire(monkeypatch, {
            "base_sha": "a" * 40, "tier": 1, "refresh_failures": 3,
            "pinned_at": "2026-07-20T12:00:00+00:00"})
        monkeypatch.setattr(verify_worker.verify_driver, "resolve_base_sha",
                            lambda: "a" * 40)
        verify_worker.maybe_refresh_base()
        assert st.reg["refresh_ok"] is True
        assert st.reg["refresh_failures"] == 0

    def test_preamble_failure_is_contained(self, monkeypatch):
        """A preamble failure (the store is unreachable) does not escape
        maybe_refresh_base — the drain tick must always reach next_queued()."""
        def boom():
            raise RuntimeError("store unavailable")
        monkeypatch.setattr(verify_worker.data, "store", boom)
        # Should not raise
        verify_worker.maybe_refresh_base()


class TestRefreshConfirmsThePin:
    def _wire(self, monkeypatch, reg):
        st = FakeStore(reg)
        monkeypatch.setattr(verify_worker.data, "store", lambda: st)
        return st

    def test_an_unmoved_head_confirms_the_pin(self, monkeypatch):
        st = self._wire(monkeypatch, {
            "base_sha": "a" * 40, "tier": 1,
            "pinned_at": "2026-07-20T12:00:00+00:00"})
        monkeypatch.setattr(verify_worker.verify_driver, "resolve_base_sha",
                            lambda: "a" * 40)
        verify_worker.maybe_refresh_base()
        assert st.reg["confirmed_at"] > "2026-07-20T12:00:00+00:00"

    def test_a_failed_refresh_keeps_the_last_confirmation(self, monkeypatch):
        st = self._wire(monkeypatch, {
            "base_sha": "a" * 40, "tier": 1,
            "pinned_at": "2026-07-20T12:00:00+00:00",
            "confirmed_at": "2026-07-21T12:00:00+00:00"})
        monkeypatch.setattr(verify_worker.verify_driver, "resolve_base_sha",
                            lambda: "b" * 40)
        monkeypatch.setattr(verify_worker.verify_driver, "prepare_base",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        verify_worker.maybe_refresh_base()
        assert st.reg["confirmed_at"] == "2026-07-21T12:00:00+00:00"


class TestStalePinHoldsTheLane:
    """A machine whose pin has fallen behind the default branch verifies
    nothing: its verify lane trips, and the self-test keeps it shut until the
    pin is current again."""
    STALE = "2026-07-01T00:00:00+00:00"

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        st = S.Store(tmp_path / "store")
        monkeypatch.setattr(verify_worker.data, "_store", st)
        monkeypatch.setattr(lane_health, "note_account", lambda: None)
        return st

    def _pin(self, store, pinned_at, **extra):
        store.save_verify_base({"host": settings.worker_id(), "base_sha": "a" * 40,
                                "tier": 1, "pinned_at": pinned_at,
                                "baseline_failing": [], "baseline_captured_at": pinned_at,
                                **extra})

    def _verify_lane(self, store):
        return worker_health.lane(worker_health.load(store, settings.worker_id()), "verify")

    def test_a_stale_pin_trips_the_verify_lane(self, store):
        self._pin(store, self.STALE, refresh_error="BuildFailure: cloning exited 128")
        verify_worker.hold_stale_pin()
        tripped = self._verify_lane(store)["tripped"]
        assert tripped["kind"] == "pin-stale"
        assert "cloning exited 128" in tripped["reason"]
        assert lane_health.open_or_retest("verify") is False

    def test_a_current_pin_leaves_the_lane_open(self, store):
        self._pin(store, _now())
        verify_worker.hold_stale_pin()
        assert self._verify_lane(store)["tripped"] is None

    def test_a_lane_already_tripped_keeps_its_cause(self, store):
        self._pin(store, self.STALE)
        worker_health.update(store, settings.worker_id(), lambda r: worker_health.trip(
            r, "verify", kind="sandbox", reason="the daemon is down"))
        verify_worker.hold_stale_pin()
        assert self._verify_lane(store)["tripped"]["kind"] == "sandbox"

    @pytest.mark.parametrize("kind", ["pin-stale", "pin-refresh"])
    def test_the_self_test_keeps_a_stale_pins_lane_shut(self, store, monkeypatch, kind):
        self._pin(store, self.STALE)
        worker_health.update(store, settings.worker_id(), lambda r: worker_health.trip(
            r, "verify", kind=kind, reason="x"))
        monkeypatch.setattr(lane_health.worker_health, "retest_due",
                            lambda rec, name, now=None: True)
        monkeypatch.setattr(worker_selftest, "sandbox_ready", lambda: None)
        assert lane_health.open_or_retest("verify") is False
        assert self._verify_lane(store)["retest"]["ok"] is False

    def test_a_rebuilt_pin_reopens_the_lane(self, store, monkeypatch):
        self._pin(store, _now())
        worker_health.update(store, settings.worker_id(), lambda r: worker_health.trip(
            r, "verify", kind="pin-stale", reason="x"))
        monkeypatch.setattr(lane_health.worker_health, "retest_due",
                            lambda rec, name, now=None: True)
        monkeypatch.setattr(worker_selftest, "sandbox_ready", lambda: None)
        assert lane_health.open_or_retest("verify") is True
