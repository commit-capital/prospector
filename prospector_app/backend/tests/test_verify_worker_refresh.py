"""The daily verify-base pin refresh: due-ness is pure; the attempt stamps
once per day, keeps the old pin on failure, and ledgers every attempt."""
from datetime import datetime, timezone

from prospector_app.backend import verify_worker


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
    def __init__(self, reg):
        self.reg = reg
        self.runs = []

    def load_verify_base(self):
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
                "prepared_on": "mac-studio",
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

    def test_preamble_failure_is_contained(self, monkeypatch):
        """A preamble failure (load_verify_base raises) does not escape
        maybe_refresh_base — the drain tick must always reach next_queued()."""
        def boom():
            raise RuntimeError("store unavailable")
        monkeypatch.setattr(verify_worker.data, "store", boom)
        # Should not raise
        verify_worker.maybe_refresh_base()
