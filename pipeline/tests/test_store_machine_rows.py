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
