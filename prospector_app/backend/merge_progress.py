"""The live merges this app process is running, and the step each has reached.

A live merge holds the request open through its compile preflight, which on a
cold base builds a Docker image and can take many minutes. The action bar polls
`get` while its click is in flight and shows the step named here, so a running
merge never reads as a click that did nothing. The record lives in memory and
only for the length of the request.
"""
from __future__ import annotations

import threading
from typing import TypedDict

from pipeline import storekit


class MergeProgress(TypedDict):
    pr: int
    head_sha: str | None
    started_at: str
    step: str
    step_at: str


_lock = threading.Lock()
_running: dict[int, MergeProgress] = {}


def start(pr: int, head_sha: str | None) -> bool:
    """Register a live merge of `pr`; False when one is already running here."""
    with _lock:
        if pr in _running:
            return False
        at = storekit.now()
        _running[pr] = {"pr": pr, "head_sha": head_sha, "started_at": at,
                        "step": "checking the merge gate", "step_at": at}
        return True


def step(pr: int, what: str) -> None:
    with _lock:
        entry = _running.get(pr)
        if entry is not None:
            entry["step"] = what
            entry["step_at"] = storekit.now()


def finish(pr: int) -> None:
    with _lock:
        _running.pop(pr, None)


def get(pr: int) -> MergeProgress | None:
    with _lock:
        entry = _running.get(pr)
        return MergeProgress(**entry) if entry is not None else None
