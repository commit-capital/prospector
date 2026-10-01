"""Progress lines for long-running steps: what a step is doing, how far along it
is, how long it has taken and how long it has left, printed to stdout.

The Control tab's job console shows a job's stdout as it arrives, so a step that
prints nothing for minutes reads as a hung job. `Progress` reports a counted
loop; `store_read` reports a bulk store read, which over a slow link to a shared
database can take minutes on its own. Store reads report only in a process that
sets PROSPECTOR_PROGRESS — the job runner sets it for every job — because the
app server makes the same reads for its own snapshot.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")

ENV = "PROSPECTOR_PROGRESS"
# A progress line is printed at most this often, besides the first and last.
EVERY_SECONDS = 3.0
# A store read of fewer rows than this is quick enough to go unreported.
STORE_READ_MIN_ROWS = 200

_clock: Callable[[], float] = time.monotonic

_NOUNS = {
    "prs": "PRs", "issues": "issues", "clusters": "clusters",
    "issue_clusters": "issue clusters", "alerts": "alerts",
    "advisories": "advisories", "diffs": "diffs", "runs": "ledger runs",
}


def say(line: str) -> None:
    print(line, flush=True)


def duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


class Progress:
    """Counts a step through `total` items. Prints "<verb> <total> <noun>…" on
    creation, "<verb> <noun>: i of N (p%) · elapsed · ~left" at most every
    EVERY_SECONDS as items advance, and a closing line from `finish`. A step of
    no items prints nothing."""

    def __init__(self, verb: str, total: int, noun: str, *, one: str | None = None,
                 indent: str = "  ") -> None:
        self.verb = verb
        self.total = total
        self.noun = noun
        self.indent = indent
        self.done = 0
        self.started = _clock()
        self._last_print = self.started
        if total:
            say(f"{indent}{verb} {total:,} {one if total == 1 and one else noun}…")

    def advance(self, n: int = 1) -> None:
        self.done += n
        now = _clock()
        if now - self._last_print < EVERY_SECONDS or self.done >= self.total:
            return
        self._last_print = now
        elapsed = now - self.started
        pct = self.done * 100 // max(1, self.total)
        left = elapsed / self.done * (self.total - self.done) if self.done else None
        eta = f" · ~{duration(left)} left" if left is not None else ""
        say(f"{self.indent}{self.verb} {self.noun}: {self.done:,} of {self.total:,} "
            f"({pct}%) · {duration(elapsed)}{eta}")

    def finish(self, summary: str | None = None) -> None:
        if not self.total:
            return
        elapsed = duration(_clock() - self.started)
        count = (f"{self.done:,}" if self.done == self.total
                 else f"{self.done:,} of {self.total:,}")
        tail = f" — {summary}" if summary else ""
        say(f"{self.indent}{self.verb} {self.noun}: done, {count} in {elapsed}{tail}")


def track(items: Iterable[T], verb: str, noun: str, *, indent: str = "  ") -> Iterator[T]:
    """Yield `items`, reporting them through a Progress that finishes when the
    iteration does."""
    seq = list(items)
    p = Progress(verb, len(seq), noun, indent=indent)
    for item in seq:
        yield item
        p.advance()
    p.finish()


def enabled() -> bool:
    return bool(os.environ.get(ENV))


def store_read(table: str, count: Callable[[], int], verb: str = "downloading") -> Progress | None:
    """A Progress for a bulk read of `table`, or None when reads go unreported
    here or the table is small. `count` is asked only when reporting is on."""
    if not enabled():
        return None
    total = count()
    if total < STORE_READ_MIN_ROWS:
        return None
    return Progress(verb, total, f"{_NOUNS.get(table, table)} from the store")
