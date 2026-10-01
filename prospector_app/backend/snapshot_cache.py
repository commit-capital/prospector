"""On-disk copies of the in-memory store snapshots, so a restarted app reads
only what changed since its last copy instead of every record.

A copy is keyed by the store it came from, the repository, the store schema
version and FORMAT, so a copy from another deployment or an older record shape
is never read. It holds the records and the watermark they are current to;
the caller reads `since` that watermark and drops ids the store no longer has.
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
from pathlib import Path
from typing import NamedTuple

from pipeline import schema
from pipeline import settings

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


def load(name: str, store_url: str) -> Copy | None:
    path = _path(name, store_url)
    if path is None:
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        records = {int(n): rec for n, rec in doc["records"].items()}
        watermark = doc["watermark"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if watermark is not None and not isinstance(watermark, str):
        return None
    return Copy(watermark, records)


def save(name: str, store_url: str, watermark: str | None, records: dict[int, dict]) -> bool:
    """Replace the copy whole from a temporary sibling; False when it could not."""
    path = _path(name, store_url)
    if path is None:
        return False
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"watermark": watermark,
                           "records": {str(n): rec for n, rec in records.items()}},
                          fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except (OSError, TypeError, ValueError):
        return False
    return True
