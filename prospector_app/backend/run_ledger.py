"""An in-memory copy of one append-only ledger table — a runs ledger or the
activity log — kept current by reading only the rows it does not hold.

The ledger is append-only. A read asks for the rows past the newest one held,
less OVERLAP rowids — a row whose insert committed after a higher rowid's lands
inside that overlap — and names the overlap rows it already holds, so a read
that finds nothing new transfers no records. Callers that arrive while a read
is in flight wait and share the next read, so every caller is answered by a
read that started after it arrived.

With a disk copy (`LedgerCopy`) the first read starts from the copy's rows and
reads the rows past them the same way. The copy outlives the process and the
store it was taken from may since have been wiped and reseeded at the same
address, so that read also asks for the copy's newest row and takes the copy
only when the store answers it with the same `ts`; otherwise it reads the whole
ledger. When a read leaves the ledger holding rows the copy does not, the copy
is written again on a background thread, at most once per COPY_EVERY.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Protocol

from pipeline import storekit

OVERLAP = 200
COPY_EVERY = 300.0  # seconds


class LedgerSource[R](Protocol):
    def runs_after(self, rowid: int | None,
                   held: Iterable[int] = ()) -> list[storekit.LedgerRow[R]]: ...


class LedgerCopy[R](Protocol):
    def load(self) -> list[storekit.LedgerRow[R]] | None:
        """The copy's rows, oldest first; None when there is no usable copy."""
        ...

    def save(self, rows: list[storekit.LedgerRow[R]]) -> bool:
        """Replace the copy with `rows`; False when it could not."""
        ...


class RunLedger[R]:
    def __init__(self, source: LedgerSource[R], copy: LedgerCopy[R] | None = None) -> None:
        self.source = source
        self.copy = copy
        self._rows: dict[int, storekit.LedgerRow[R]] = {}
        self._loaded = False
        self._cond = threading.Condition()
        self._reading = False
        self._started = 0  # reads begun
        self._finished = 0  # the number of the newest read that published
        self._copied: int | None = None  # how many rows the disk copy holds, when known
        self._copy_at: float | None = None  # monotonic time the copy was last written or taken
        self._copier: threading.Thread | None = None

    @property
    def loaded(self) -> bool:
        """Whether a read has completed, so the copy holds the whole ledger."""
        return self._loaded

    def rows(self) -> list[storekit.LedgerRow[R]]:
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
            rows = self._read_past(held) if self._loaded else self._first_read()
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
            self._copy_if_changed()
        return list(rows.values())

    def _first_read(self) -> dict[int, storekit.LedgerRow[R]]:
        """The whole ledger: the disk copy and the rows past it when the source
        still has the copy's newest row with its `ts`, else every row."""
        seed = self.copy.load() if self.copy is not None else None
        if seed:
            held = {r.rowid: r for r in seed}
            newest = seed[-1]
            new = self._read_after(held, ask_newest=True)
            if any(r.rowid == newest.rowid and r.ts == newest.ts for r in new):
                with self._cond:
                    self._copied, self._copy_at = len(held), time.monotonic()
                return self._merge(held, new)
        return self._read_past({})

    def _read_past(self, held: dict[int, storekit.LedgerRow[R]]) -> dict[int, storekit.LedgerRow[R]]:
        """`held` with the rows the source has and it lacks, in rowid order."""
        return self._merge(held, self._read_after(held, ask_newest=False))

    def _read_after(self, held: dict[int, storekit.LedgerRow[R]], *,
                    ask_newest: bool) -> list[storekit.LedgerRow[R]]:
        """The source's rows past `held`'s newest less OVERLAP, without the ones
        `held` has — but with its newest when `ask_newest`."""
        newest = max(held) if held else None
        after = newest - OVERLAP if newest is not None else None
        skip = [n for n in held
                if after is not None and n > after and not (ask_newest and n == newest)]
        return self.source.runs_after(after, skip)

    @staticmethod
    def _merge(held: dict[int, storekit.LedgerRow[R]],
               new: list[storekit.LedgerRow[R]]) -> dict[int, storekit.LedgerRow[R]]:
        if not new:
            return held
        rows = {**held, **{r.rowid: r for r in new}}
        if held and new[0].rowid < next(reversed(held)):
            rows = dict(sorted(rows.items()))
        return rows

    def _copy_if_changed(self) -> None:
        """Under `_cond`: start writing the disk copy when its row count is not
        the held ledger's and COPY_EVERY has passed since it was written."""
        if (self.copy is None or self._copier is not None
                or len(self._rows) == self._copied
                or (self._copy_at is not None and time.monotonic() - self._copy_at < COPY_EVERY)):
            return
        self._copier = threading.Thread(
            target=self._write_copy, args=(self.copy, list(self._rows.values())),
            daemon=True, name="run-ledger-copy")
        self._copier.start()

    def _write_copy(self, copy: LedgerCopy[R], rows: list[storekit.LedgerRow[R]]) -> None:
        saved = False
        try:
            saved = copy.save(rows)
        finally:
            with self._cond:
                self._copier = None
                self._copy_at = time.monotonic()
                if saved:
                    self._copied = len(rows)
