"""An in-memory copy of one runs ledger, kept current by reading only the rows
it does not hold.

The ledger is append-only. A read asks for the rows past the newest one held,
less OVERLAP rowids — a row whose insert committed after a higher rowid's lands
inside that overlap — and names the overlap rows it already holds, so a read
that finds nothing new transfers no records. Callers that arrive while a read
is in flight wait and share the next read, so every caller is answered by a
read that started after it arrived.
"""
from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Protocol

from pipeline import storekit

OVERLAP = 200


class LedgerSource(Protocol):
    def runs_after(self, rowid: int | None,
                   held: Iterable[int] = ()) -> list[storekit.LedgerRow]: ...


class RunLedger:
    def __init__(self, source: LedgerSource) -> None:
        self.source = source
        self._rows: dict[int, storekit.LedgerRow] = {}
        self._loaded = False
        self._cond = threading.Condition()
        self._reading = False
        self._started = 0  # reads begun
        self._finished = 0  # the number of the newest read that published

    @property
    def loaded(self) -> bool:
        """Whether a read has completed, so the copy holds the whole ledger."""
        return self._loaded

    def rows(self) -> list[storekit.LedgerRow]:
        """Every row, oldest first, as of a read that started after this call."""
        with self._cond:
            arrival = self._started
            while self._reading:
                self._cond.wait()
            if self._finished > arrival:
                return list(self._rows.values())
            self._reading = True
            self._started += 1
            mine = self._started
            held = self._rows
        try:
            rows = self._read_past(held)
        except BaseException:
            with self._cond:
                self._reading = False
                self._cond.notify_all()
            raise
        with self._cond:
            self._rows = rows
            self._loaded = True
            self._finished = mine
            self._reading = False
            self._cond.notify_all()
        return list(rows.values())

    def _read_past(self, held: dict[int, storekit.LedgerRow]) -> dict[int, storekit.LedgerRow]:
        """`held` with the rows the source has and it lacks, in rowid order."""
        after = max(held) - OVERLAP if held else None
        overlap = [n for n in held if after is not None and n > after]
        new = self.source.runs_after(after, overlap)
        if not new:
            return held
        rows = {**held, **{r.rowid: r for r in new}}
        if held and new[0].rowid < next(reversed(held)):
            rows = dict(sorted(rows.items()))
        return rows
