"""A wave of agent batches stops at the first usage-limit hit or closed capacity
gate: no batch starts its agent after one of them is refused, agents already
running finish, and the job prints one line saying why it stopped.

The stop is decided on the worker thread that saw the refusal, before that
thread can take the next queued batch, so a pool of one runs nothing more.
"""
from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future
from datetime import datetime
from typing import NamedTuple, ParamSpec, TypeVar

from pipeline import capacity
from pipeline import headless_agent

P = ParamSpec("P")
R = TypeVar("R")

STOPS = (headless_agent.CapacityExhausted, capacity.CapacityPaused)


class NotStarted(Exception):
    """The batch's agent never ran: an earlier batch stopped the wave."""


class Stop(NamedTuple):
    line: str
    exit_code: int


def _local(at: datetime) -> str:
    return at.astimezone().strftime("%H:%M")


def stop_reason(exc: BaseException, not_started: int) -> Stop | None:
    """Why `exc` stops the wave and the job's exit code: 1 for a spent usage
    limit, 0 for a closed capacity gate (a deferral). None for any other
    exception, which fails only its own batch."""
    if isinstance(exc, headless_agent.CapacityExhausted):
        when = f"resets at {_local(exc.resets_at)}" if exc.resets_at else "resets later"
        return Stop(f"AI usage limit reached — {when} ({not_started} batch(es) not "
                    "started); stopping.", 1)
    if isinstance(exc, capacity.CapacityPaused):
        retry = exc.decision.retry_at
        when = f"retry ~{_local(retry)}" if retry else "retry later"
        return Stop(f"AI capacity paused: {exc.decision.reason} — {when}; stopping.", 0)
    return None


class Wave:
    """The batches of one wave. Each batch submitted through `submit` runs its
    agent only while no batch has been refused by the usage limit or the
    capacity gate; after that it raises NotStarted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._submitted = 0
        self._started = 0
        self._stopped = False

    def submit(self, pool: Executor, fn: Callable[P, R], /,
               *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        with self._lock:
            self._submitted += 1
        return pool.submit(self._run, functools.partial(fn, *args, **kwargs))

    def not_started(self) -> int:
        with self._lock:
            return self._submitted - self._started

    def _run(self, call: Callable[[], R]) -> R:
        with self._lock:
            if self._stopped:
                raise NotStarted
            self._started += 1
        try:
            return call()
        except STOPS:
            with self._lock:
                self._stopped = True
            raise
