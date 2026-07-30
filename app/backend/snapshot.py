"""Small helper for lazy, debounced in-process store snapshots.

The PR and Issue read paths both want the same control flow: one blocking cold
load on first use, then non-blocking incremental refreshes after a debounce.
Callers own the actual data/watermark shape; this class owns only the locking
and scheduling.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class LazySnapshot:
    def __init__(self, freshen: Callable[[bool], None], *, debounce: float = 10.0):
        self._freshen = freshen
        self._debounce = debounce
        self._loaded = False
        self._last_check = 0.0
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    def ensure(self) -> None:
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self._freshen(True)
                    self._loaded = True
                    self._last_check = time.monotonic()
            return
        if time.monotonic() - self._last_check < self._debounce:
            return
        if not self._lock.acquire(blocking=False):
            return
        self._last_check = time.monotonic()

        def run() -> None:
            try:
                self._freshen(False)
            except Exception:
                pass
            finally:
                self._lock.release()

        threading.Thread(target=run, daemon=True, name="store-snapshot-freshen").start()

    def refresh(self) -> None:
        with self._lock:
            self._freshen(not self._loaded)
            self._loaded = True
            self._last_check = time.monotonic()

    def invalidate(self) -> None:
        with self._lock:
            self._loaded = False
            self._last_check = 0.0
