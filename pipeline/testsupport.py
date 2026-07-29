"""Test-only store helpers, importable by bare name off the registered pipeline
source root (so the test suites reach them without any sys.path setup)."""
from __future__ import annotations

from pipeline.store import Store
from pipeline.storekit import now as _now


def set_section(store: Store, n: int, section: str, payload: dict,
                against_head_sha: str | None = None) -> dict:
    """Plant a fact section directly onto a stored PR. Writes through .raw — the
    sanctioned raw-dict access for tests — stamping checked_at, and (for non-meta
    sections) against_head_sha so the section reads as current."""
    pr = store.load_pr(n)
    if pr is None:
        raise KeyError(f"PR {n} not in store")
    rec = pr.raw
    stamped = dict(payload)
    stamped["checked_at"] = _now()
    if section != "meta":
        stamped["against_head_sha"] = against_head_sha or rec["meta"]["head_sha"]
    rec[section] = stamped
    store.save_pr(rec)
    return rec
