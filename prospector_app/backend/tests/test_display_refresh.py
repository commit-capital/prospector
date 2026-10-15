"""The display lane writes cards for analyzed PRs within its daily budget and
stops on a closed capacity gate."""
from __future__ import annotations

import pytest

from pipeline import capacity, display_pass
from pipeline import store as S
from prospector_app.backend import data, display_refresh


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(data, "prs", st.all_prs)
    monkeypatch.setattr(data, "refresh", lambda: None)
    for n in (1, 2, 3):
        st.create_pr(n, {"title": f"pr {n}", "state": "open", "head_sha": "h1"})
        st.edit_pr(n).route_to("close-stale", "Abandoned.")
    return st


def _write(written: list[int]):
    def write(store, n, *, trigger):
        written.append(n)
        store.append_run({"phase": display_pass.PHASE, "pr": n, "started": "x", "finished": "x",
                          "trigger": trigger, "stats": {}})
        return {}
    return write


def test_a_pass_stops_at_the_daily_budget(store, monkeypatch):
    monkeypatch.setenv("TRIAGE_DISPLAY_DAILY", "2")
    written: list[int] = []
    monkeypatch.setattr(display_pass, "write", _write(written))
    assert display_refresh.pass_once() == [1, 2]
    assert display_refresh.pass_once() == []
    assert written == [1, 2]


def test_a_closed_gate_ends_the_pass(store, monkeypatch):
    def paused(store, n, *, trigger):
        raise capacity.CapacityPaused(capacity.Decision(False, "paused", None))
    monkeypatch.setattr(display_pass, "write", paused)
    assert display_refresh.pass_once() == []
