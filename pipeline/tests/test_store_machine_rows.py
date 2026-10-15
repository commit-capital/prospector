"""Per-host machine-mode and worker-loops registry rows."""
from __future__ import annotations

import pytest

from pipeline import store as S


@pytest.fixture
def st(tmp_path):
    return S.Store(tmp_path / "store")


def test_machine_mode_round_trips_and_clears(st):
    st.save_machine_mode({"host": "mac-1", "stood_down": True, "at": "2026-10-14T10:00:00+00:00",
                          "by": "ops", "note": "laptop"})
    assert st.load_machine_modes() == {"mac-1": {"host": "mac-1", "stood_down": True,
                                                 "at": "2026-10-14T10:00:00+00:00",
                                                 "by": "ops", "note": "laptop"}}
    assert st.clear_machine_mode("mac-1") is True
    assert st.load_machine_modes() == {}
    assert st.clear_machine_mode("mac-1") is False


def test_worker_loops_one_row_per_host(st):
    st.save_worker_loops({"host": "a", "at": "t1", "lanes": {"verify": True}, "loops": {}})
    st.save_worker_loops({"host": "b", "at": "t2", "lanes": {}, "loops": {}})
    st.save_worker_loops({"host": "a", "at": "t3", "lanes": {"verify": False}, "loops": {}})
    rows = st.load_worker_loops()
    assert set(rows) == {"a", "b"}
    assert rows["a"]["at"] == "t3" and rows["a"]["lanes"] == {"verify": False}


def test_rows_require_a_host(st):
    with pytest.raises(S.ValidationError):
        st.save_machine_mode({"stood_down": True})
    with pytest.raises(S.ValidationError):
        st.save_worker_loops({"lanes": {}})


def test_new_rows_do_not_leak_into_worker_health(st):
    st.save_machine_mode({"host": "mac-1", "stood_down": True, "at": "t"})
    st.save_worker_loops({"host": "mac-1", "at": "t", "lanes": {}, "loops": {}})
    assert st.load_worker_health() == {"hosts": {}}


def test_lane_pause_round_trips(st):
    st.save_lane_pause({"host": "mac-1", "lanes": {"verify": {"at": "t", "by": "ops"}}})
    assert st.load_lane_pauses() == {"mac-1": {"host": "mac-1",
                                               "lanes": {"verify": {"at": "t", "by": "ops"}}}}
    with pytest.raises(S.ValidationError):
        st.save_lane_pause({"lanes": {}})


def _queued(st, n: int, section: str, status: str = "queued") -> None:
    st.save_pr({"pr": n, "meta": {"title": f"pr {n}", "state": "open", "head_sha": "a" * 40},
                section: {"status": status, "queued_at": f"2026-10-0{n}T00:00:00+00:00",
                          "source": "operator", **({"action": "rebase"} if section == "fix_request" else {})}})


def test_prioritize_sets_priority_on_a_waiting_request(st):
    _queued(st, 1, "verify_request")
    _queued(st, 2, "fix_request")
    assert st.prioritize_verify_request(1, 3.0) is True
    assert st.prioritize_fix_request(2, 1.0) is True
    assert st.load_pr(1).verify_request["priority"] == 3.0
    assert st.load_pr(2).fix_request["priority"] == 1.0
    assert st.load_pr(2).fix_request["status"] == "queued"


def test_prioritize_refuses_a_request_that_is_not_waiting(st):
    _queued(st, 1, "verify_request", status="running")
    assert st.prioritize_verify_request(1, 3.0) is False
    assert "priority" not in st.load_pr(1).verify_request


def test_prioritize_refuses_a_claimed_request(st):
    _queued(st, 1, "verify_request")
    real = st._prs.stamped
    raced = []

    def racing(n):
        row = real(n)
        if not raced:
            raced.append(1)
            assert st.claim_verify_request(n, host="worker") is not None
        return row
    st._prs.stamped = racing
    assert st.prioritize_verify_request(1, 5.0) is False
    st._prs.stamped = real
    req = st.load_pr(1).verify_request
    assert req["status"] == "running" and "priority" not in req
