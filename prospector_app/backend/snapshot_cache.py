"""On-disk copies of the in-memory store snapshots, so a restarted app reads
only what changed since its last copy instead of every record.

A copy is keyed by the store it came from, the repository, the store schema
version and FORMAT, so a copy from another deployment or an older record shape
is never read. It holds the records and the watermark they are current to;
the caller reads `since` that watermark and drops ids the store no longer has.
A ledger's copy (`LedgerFile`) holds each row's rowid, `ts` and record.
Files live owner-only under PROSPECTOR_CACHE_DIR (default ~/.cache/prospector).
Every failure to read or write a copy is a miss: the caller loads from the
store as it would with no copy.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from pipeline import schema
from pipeline import settings
from pipeline import storekit

FORMAT = 1


class Copy(NamedTuple):
    watermark: str | None
    records: dict[int, dict]


def cache_dir() -> Path | None:
    """Where copies live; None under pytest unless PROSPECTOR_CACHE_DIR names a
    directory, so a test never reads a real deployment's copy."""
    configured = os.environ.get("PROSPECTOR_CACHE_DIR")
    if configured:
        return Path(configured)
    if "pytest" in sys.modules:
        return None
    return Path.home() / ".cache" / "prospector"


def _path(name: str, store_url: str) -> Path | None:
    root = cache_dir()
    if root is None:
        return None
    key = "|".join([store_url, settings.repo() or "", str(schema.STORE_SCHEMA_VERSION),
                    str(FORMAT), name])
    return root / f"{name}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.json"


def _read(name: str, store_url: str) -> dict | None:
    path = _path(name, store_url)
    if path is None:
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _write(name: str, store_url: str, doc: dict) -> bool:
    """Replace the copy whole from a temporary sibling; False when it could not."""
    path = _path(name, store_url)
    if path is None:
        return False
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except (OSError, TypeError, ValueError):
        return False
    return True


def load(name: str, store_url: str) -> Copy | None:
    doc = _read(name, store_url)
    if doc is None:
        return None
    try:
        records = {int(n): rec for n, rec in doc["records"].items()}
        watermark = doc["watermark"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if watermark is not None and not isinstance(watermark, str):
        return None
    return Copy(watermark, records)


def save(name: str, store_url: str, watermark: str | None, records: dict[int, dict]) -> bool:
    return _write(name, store_url, {"watermark": watermark,
                                    "records": {str(n): rec for n, rec in records.items()}})


class LedgerFile[R]:
    """The disk copy of one ledger (`run_ledger.LedgerCopy`): its rows in rowid
    order, each record written as `raw` gives it and read back through `parse`."""

    def __init__(self, name: str, store_url: str,
                 parse: Callable[[dict], R], raw: Callable[[R], dict]) -> None:
        self.name = name
        self.store_url = store_url
        self.parse = parse
        self.raw = raw

    def load(self) -> list[storekit.LedgerRow[R]] | None:
        """The copy's rows, oldest first; None for a copy that is absent,
        unreadable, or not in strictly ascending rowid order."""
        doc = _read(self.name, self.store_url)
        if doc is None:
            return None
        try:
            rows = [storekit.LedgerRow(rowid, ts, self.parse(rec))
                    for rowid, ts, rec in doc["rows"]]
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        last: int | None = None
        for row in rows:
            if (not isinstance(row.rowid, int)
                    or (row.ts is not None and not isinstance(row.ts, str))
                    or (last is not None and row.rowid <= last)):
                return None
            last = row.rowid
        return rows

    def save(self, rows: list[storekit.LedgerRow[R]]) -> bool:
        return _write(self.name, self.store_url,
                      {"rows": [[r.rowid, r.ts, self.raw(r.record)] for r in rows]})


def runs_file(name: str, store_url: str) -> LedgerFile[storekit.RunRecord]:
    """The disk copy of one runs ledger."""
    return LedgerFile(name, store_url, storekit.parse_run, lambda r: r.raw)
