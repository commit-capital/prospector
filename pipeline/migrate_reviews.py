"""Lift a PR record's legacy Greptile signals into its `reviews` section.

A store written before the `reviews` section existed keeps Greptile's score
and reviewed commit under `signals.greptile` / `signals.greptile_reviewed_sha`.
This transform moves them into a `reviews.greptile` entry stamped against the
head the signals were read at, so closed-PR history keeps its score and an
open PR reads correctly until the next ingest rewrites it. Runs through
`pipeline.store_edit` (dry-run by default, pre-image snapshot, runs-ledger
entry):

    uv run python pipeline/migrate_reviews.py            # dry run
    uv run python pipeline/migrate_reviews.py --live     # apply
"""
from __future__ import annotations

import sys

from pipeline import store_edit


def lift_greptile_signals(rec: dict) -> dict | None:
    """The record with `signals.greptile*` moved into `reviews.greptile`, or None
    when there is nothing to move (no legacy keys, or an entry already stored)."""
    sig = rec.get("signals") or {}
    if "greptile" not in sig and "greptile_reviewed_sha" not in sig:
        return None
    score = sig.pop("greptile", None)
    reviewed = sig.pop("greptile_reviewed_sha", None)
    reviews = dict(rec.get("reviews") or {})
    if "greptile" not in reviews:
        reviews["greptile"] = {"kind": "review", "score": score, "reviewed_sha": reviewed,
                               "observed_at": sig.get("checked_at"), "findings": [],
                               "summary": None, "checks": [], "extra": {}}
        reviews.setdefault("checked_at", sig.get("checked_at"))
        reviews.setdefault("against_head_sha", sig.get("against_head_sha"))
    rec["reviews"] = reviews
    return rec


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    store_edit.main(["pipeline.migrate_reviews:lift_greptile_signals", "--table", "prs", *args])


if __name__ == "__main__":
    main()
