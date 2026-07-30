"""Decision-capture for future agent learning.

Every human review action records a structured row: the PR's FEATURES at the
moment of decision + the DECISION the human made + an optional PRIVATE reason
("why"). This is the labelled dataset we want — features → human label + rationale
— to later teach an agent which PRs we merge/close and why.

Private reasons are NEVER posted to GitHub; they live only in the store's
training_decisions table. The public comment/review body (which IS posted) is
also stored alongside, so the dataset has both the private "real" reason and the
diplomatic public message.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, insert, select

from pipeline import schema
from pipeline import storekit
from prospector_app.backend import service

# The pre-store decision log; retained on disk so a one-time backfill can read
# it into the store.
LOG = Path(__file__).resolve().parents[1] / "training" / "decisions.jsonl"

# pipeline/ holds the default SQLite store when TRIAGE_STORE_URL is unset.
_DEFAULT_STORE_ROOT = Path(__file__).resolve().parents[2] / "pipeline" / "store"

# Test hook: monkeypatch.setattr(training, "_TEST_ENGINE", eng) in fixtures.
_TEST_ENGINE: Engine | None = None
_table_ensured: bool = False


def _engine() -> Engine:
    global _table_ensured
    if _TEST_ENGINE is not None:
        return _TEST_ENGINE
    url = os.environ.get("TRIAGE_STORE_URL") or f"sqlite:///{_DEFAULT_STORE_ROOT}/store.db"
    eng = storekit.get_engine(url)
    if not _table_ensured:
        with eng.begin() as conn:
            schema.training_decisions.create(conn, checkfirst=True)
        _table_ensured = True
    return eng


def _features(pr: int) -> dict:
    row = service.pr_row(int(pr)) or {}
    signals = row.get("signals") or {}
    checks = row.get("checks") or {}
    au = row.get("author_stats") or {}
    return {
        "title": row.get("title"),
        "size": (signals.get("additions") or 0) + (signals.get("deletions") or 0),
        "additions": signals.get("additions"),
        "deletions": signals.get("deletions"),
        "changed_files": signals.get("changed_files"),
        "greptile": signals.get("greptile"),
        "ci": signals.get("ci"),
        "conflicts": signals.get("conflicts"),
        "checks_passed": checks.get("passed"),
        "checks_total": checks.get("total"),
        "safety": row.get("safety"),
        "safety_findings": row.get("safety_findings"),
        "drift": row.get("drift_state"),
        "cluster": (row.get("clusters") or [None])[0],   # primary (lowest), or None
        "author": row.get("author"),
        "author_merge_rate_shrunk": au.get("merge_rate_shrunk"),
        "author_merge_rate": au.get("merge_rate"),
        "author_total_prs": au.get("total"),
        "trusted_author": row.get("trusted_author"),
    }


def capture(pr: int, decision: str, *, reason: str | None = None, tags: list | None = None,
            public_body: str | None = None, by: str = "operator", dry_run: bool = True,
            result: dict | None = None) -> dict:
    rec = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "pr": int(pr),
        "decision": decision,
        "reason_private": (reason or "").strip() or None,
        "tags": tags or [],
        "public_body": (public_body or "").strip() or None,
        "by": by,
        "dry_run": dry_run,
        "status": (result or {}).get("status"),
        "features": _features(pr),
    }
    eng = _engine()
    storekit.assert_repo(eng)
    with eng.begin() as conn:
        conn.execute(insert(schema.training_decisions)
                     .values(**schema.training_decision_row(rec)))
    return rec


def stats() -> dict:
    """How much labelled decision data we have: total, how many carry a private
    reason, and the count per decision."""
    with _engine().connect() as conn:
        rows = conn.execute(select(schema.training_decisions.c.data)).all()
    count = with_reason = 0
    decisions: dict[str, int] = {}
    for (rec,) in rows:
        count += 1
        if (rec or {}).get("reason_private"):
            with_reason += 1
        d = (rec or {}).get("decision", "?")
        decisions[d] = decisions.get(d, 0) + 1
    return {"count": count, "with_reason": with_reason, "decisions": decisions}


def import_local_log(path: Path | None = None) -> dict[str, int]:
    """Import the pre-store decision log at `path` (LOG by default) into the
    store, returning {read, imported, skipped}. Idempotent — a row already
    present, keyed on (at, pr, decision, dry_run), is skipped — so re-running is
    safe. `at` is second-resolution, so one operator acting twice on a PR inside
    the same second is distinguished only by `dry_run`: a preview and the live
    action it precedes are separate rows the corpus needs to tell apart. Reads
    the source without modifying it."""
    src = path or LOG
    if not src.exists():
        return {"read": 0, "imported": 0, "skipped": 0}
    cols = schema.training_decisions.c
    with _engine().connect() as conn:
        seen = {(r.at, r.pr, r.decision, r.dry_run) for r in
                conn.execute(select(cols.at, cols.pr, cols.decision, cols.dry_run)).all()}
    read = imported = 0
    for line in src.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        read += 1
        row = schema.training_decision_row(rec)
        key = (row["at"], row["pr"], row["decision"], row["dry_run"])
        if key in seen:
            continue
        with _engine().begin() as conn:
            conn.execute(insert(schema.training_decisions).values(**row))
        seen.add(key)
        imported += 1
    return {"read": read, "imported": imported, "skipped": read - imported}


if __name__ == "__main__":
    print(json.dumps(import_local_log(), indent=2))
