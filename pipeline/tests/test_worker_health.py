"""worker_health: the trip policy over a worker's per-lane record."""
from datetime import datetime, timedelta, timezone

from pipeline import worker_health as wh
from pipeline.store import Store

T0 = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


class TestTrip:
    def test_three_consecutive_failures_trip(self):
        rec = wh.empty("w")
        assert wh.record_failure(rec, "fix", kind="sandbox", reason="docker down") is False
        assert wh.record_failure(rec, "fix", kind="sandbox", reason="docker down") is False
        assert wh.record_failure(rec, "fix", kind="sandbox", reason="docker down") is True
        assert wh.is_tripped(rec, "fix")
        assert "3 machine failures" in rec["lanes"]["fix"]["tripped"]["reason"]

    def test_a_success_resets_the_run(self):
        rec = wh.empty("w")
        wh.record_failure(rec, "fix", kind="sandbox", reason="x")
        wh.record_failure(rec, "fix", kind="sandbox", reason="x")
        wh.record_success(rec, "fix")
        assert wh.record_failure(rec, "fix", kind="sandbox", reason="x") is False
        assert not wh.is_tripped(rec, "fix")

    def test_lanes_are_independent(self):
        rec = wh.empty("w")
        for _ in range(3):
            wh.record_failure(rec, "verify", kind="sandbox", reason="x")
        assert wh.is_tripped(rec, "verify") and not wh.is_tripped(rec, "fix")

    def test_an_already_tripped_lane_reports_no_new_trip(self):
        rec = wh.empty("w")
        assert wh.trip(rec, "security", kind="agent-unavailable", reason="401") is True
        assert wh.trip(rec, "security", kind="agent-unavailable", reason="401") is False
        assert wh.record_failure(rec, "security", kind="x", reason="y") is False

    def test_recent_keeps_the_last_few(self):
        rec = wh.empty("w")
        for i in range(8):
            wh.record_failure(rec, "fix", kind="k", reason=f"r{i}", pr=i)
        assert [f["pr"] for f in rec["lanes"]["fix"]["recent"]] == [3, 4, 5, 6, 7]

    def test_reopen_clears_the_trip_and_the_count(self):
        rec = wh.empty("w")
        for _ in range(3):
            wh.record_failure(rec, "fix", kind="k", reason="r")
        wh.reopen(rec, "fix", by="operator")
        assert not wh.is_tripped(rec, "fix")
        assert rec["lanes"]["fix"]["consecutive_failures"] == 0


class TestRetest:
    def test_first_retest_waits_the_full_interval(self):
        rec = wh.empty("w")
        wh.trip(rec, "fix", kind="k", reason="r", now=_iso(T0))
        assert not wh.retest_due(rec, "fix", now=T0 + timedelta(seconds=1))
        assert not wh.retest_due(rec, "fix", now=T0 + timedelta(minutes=14))
        assert wh.retest_due(rec, "fix", now=T0 + timedelta(minutes=15))

    def test_cooled_after_six_hours_closed(self):
        rec = wh.empty("w")
        wh.trip(rec, "fix", kind="k", reason="r", now=_iso(T0))
        assert not wh.cooled(rec, "fix", now=T0 + timedelta(hours=5))
        assert wh.cooled(rec, "fix", now=T0 + timedelta(hours=6))
        assert not wh.cooled(wh.empty("w"), "fix", now=T0)

    def test_not_due_soon_after_a_failed_retest(self):
        rec = wh.empty("w")
        wh.trip(rec, "fix", kind="k", reason="r", now=_iso(T0))
        wh.record_retest(rec, "fix", ok=False, detail="still down", now=_iso(T0))
        assert not wh.retest_due(rec, "fix", now=T0 + timedelta(minutes=5))
        assert wh.retest_due(rec, "fix", now=T0 + timedelta(minutes=16))

    def test_an_open_lane_is_never_retested(self):
        assert not wh.retest_due(wh.empty("w"), "fix", now=T0)



def test_update_round_trips_through_the_store(tmp_path):
    st = Store(tmp_path / "db")
    wh.update(st, "w1", lambda r: wh.trip(r, "fix", kind="k", reason="r"))
    wh.update(st, "w2", lambda r: wh.record_success(r, "verify"))
    assert wh.is_tripped(wh.load(st, "w1"), "fix")
    assert set(st.load_worker_health()["hosts"]) == {"w1", "w2"}
