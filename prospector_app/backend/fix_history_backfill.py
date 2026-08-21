"""Seed the runs ledger with the autofix endings the store already holds.

The fix worker appends one ``fix:single`` ledger entry per action it finishes,
which is what the app's fix history reads. A store written before that has no
such entries — but each PR's ``fix_request`` still carries its last ending, so
the history can start with those rather than empty.

Reads and writes only the local store; nothing upstream is touched. The default
is a dry-run listing of what would be appended. Re-running is safe: an ending
already in the ledger, at the same PR and the same finished stamp, is skipped.
"""
from __future__ import annotations

import argparse
import json

from prospector_app.backend import data
from prospector_app.backend.fix_queue import FINISHED_STATUSES

FIX_PHASE = "fix:single"


def _logged() -> set[tuple[int, str]]:
    """The (pr, finished) pairs the ledger already carries a fix run for."""
    out: set[tuple[int, str]] = set()
    for rec in data.runs():
        raw = getattr(rec, "raw", None)
        if getattr(rec, "phase", None) != FIX_PHASE or raw is None:
            continue
        n, finished = raw.get("pr"), raw.get("finished")
        if isinstance(n, int) and isinstance(finished, str):
            out.add((n, finished))
    return out


def _detail(req: dict) -> str | None:
    for key in ("refused_reason", "error"):
        text = req.get(key)
        if isinstance(text, str) and text.strip():
            return text
    message = (req.get("result") or {}).get("message")
    return message if isinstance(message, str) and message.strip() else None


def _entry(n: int, req: dict) -> dict:
    return {
        "phase": FIX_PHASE, "pr": n, "started": req.get("started_at"),
        "finished": req.get("finished_at"), "backfilled": True,
        "trigger": "autohunt" if req.get("source") == "auto" else None,
        "stats": {"status": req.get("status"), "action": req.get("action"),
                  "detail": _detail(req), "host": req.get("host")}}


def pending() -> list[dict]:
    """One ledger entry per stored autofix ending the ledger does not have yet,
    oldest first. An ending needs a finished stamp to be reconstructed: it dates
    the run, and it is what makes a second pass skip this entry."""
    logged = _logged()
    out: list[dict] = []
    for n, rec in data.prs().items():
        req = rec.fix_request or {}
        finished = req.get("finished_at")
        if req.get("status") not in FINISHED_STATUSES or not isinstance(finished, str):
            continue
        if (n, finished) in logged:
            continue
        out.append(_entry(n, req))
    out.sort(key=lambda e: str(e["finished"]))
    return out


def backfill(entries: list[dict], *, live: bool) -> list[dict]:
    """Append `entries` to the runs ledger, or — with ``live=False`` — return
    them without writing. Returns the (would-be) entries."""
    if live:
        store = data.store()
        for entry in entries:
            store.append_run(entry)
    return entries


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="fix-history-backfill",
        description="Seed the runs ledger with the autofix endings each PR's "
                    "stored fix_request already carries (dry-run default).")
    p.add_argument("--live", action="store_true",
                   help="write the entries; the default prints what would be written")
    args = p.parse_args(argv)
    entries = backfill(pending(), live=args.live)
    print(json.dumps({"entries": entries, "count": len(entries),
                      "dry_run": not args.live}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
