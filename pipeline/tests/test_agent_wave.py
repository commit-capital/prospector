from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pytest

from pipeline import agent_wave
from pipeline import capacity
from pipeline import headless_agent

RESET = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
LOCAL = RESET.astimezone().strftime("%H:%M")


def test_a_spent_usage_limit_stops_with_its_reset_and_exit_1():
    exc = headless_agent.CapacityExhausted("limit", resets_at=RESET, window="five_hour")
    assert agent_wave.stop_reason(exc, 3) == (
        f"AI usage limit reached — resets at {LOCAL} (3 batch(es) not started); stopping.", 1)


def test_a_limit_without_a_reset_time_resets_later():
    exc = headless_agent.CapacityExhausted("limit")
    line, code = agent_wave.stop_reason(exc, 0) or ("", -1)
    assert line == "AI usage limit reached — resets later (0 batch(es) not started); stopping."
    assert code == 1


def test_a_closed_capacity_gate_is_a_deferral_with_exit_0():
    exc = capacity.CapacityPaused(capacity.Decision(False, "day cap 50% reached", RESET))
    assert agent_wave.stop_reason(exc, 2) == (
        f"AI capacity paused: day cap 50% reached — retry ~{LOCAL}; stopping.", 0)


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("bad json"),
                                 headless_agent.AgentUnavailable("not logged in")])
def test_any_other_failure_does_not_stop_the_wave(exc):
    assert agent_wave.stop_reason(exc, 4) is None


@pytest.mark.parametrize("stopping", [
    headless_agent.CapacityExhausted("limit", resets_at=RESET),
    capacity.CapacityPaused(capacity.Decision(False, "paused", RESET)),
])
def test_no_batch_starts_its_agent_after_one_stops_the_wave(stopping):
    ran: list[int] = []

    def agent(n: int) -> int:
        ran.append(n)
        if n == 0:
            raise stopping
        return n

    wave = agent_wave.Wave()
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = [wave.submit(pool, agent, n) for n in range(4)]
        outcomes = [type(f.exception()) for f in as_completed(futures)]
    assert ran == [0]
    assert wave.not_started() == 3
    assert outcomes.count(agent_wave.NotStarted) == 3


def test_an_ordinary_failure_leaves_the_other_batches_running():
    def agent(n: int) -> int:
        if n == 0:
            raise RuntimeError("boom")
        return n

    wave = agent_wave.Wave()
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = [wave.submit(pool, agent, n) for n in range(3)]
        results = sorted(f.result() for f in futures[1:])
    assert results == [1, 2]
    assert wave.not_started() == 0
