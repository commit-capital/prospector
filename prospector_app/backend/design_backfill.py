"""Design-review a batch of PR heads now — the Control tab's `design-review`
job. Reviews run one at a time through `verify_worker.run_design` as an
operator's run: Home's merge-ready PRs first, then the hunter's order, with no
AI capacity gate and no turn-taking with the security and verify lanes. The
batch stops after `--limit` reviews land, when the pool runs out, or after
three failures in a row. With `--pr N` it reviews that one PR's head instead,
whatever review the head already has — the PR explorer's bulk "run design
reviews", one job per PR, and the PR page's Design review re-run.

  uv run python -m prospector_app.backend.design_backfill [--limit N | --pr N]
"""
from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from pipeline import design_review
from pipeline import settings
from pipeline import store
from prospector_app.backend import automation
from prospector_app.backend import data
from prospector_app.backend import verify_worker

if TYPE_CHECKING:
    from pipeline.model import Pr

TRIGGER = "control"
ONE_TRIGGER = "bulk"
MAX_FAILURES_IN_A_ROW = 3


def candidates() -> list[int]:
    """Every head the design review may take, merge-ready PRs first."""
    me = f"{settings.worker_id()}/{TRIGGER}"
    pool = [(n, pr) for n, pr in data.prs().items()
            if design_review.huntable(pr)
            and not store.run_claim_held_elsewhere(
                pr.raw.get("design_run"), host=me,
                stale_after=verify_worker.DESIGN_CLAIM_SECONDS)]

    def key(item: tuple[int, Pr]) -> tuple[bool, tuple[bool, int, bool, bool, float, int]]:
        standing = automation.classify(item[1]) or {}
        return (standing.get("bucket") != "merge-ready", verify_worker.design_hunt_key(item))

    return [n for n, _ in sorted(pool, key=key)]


def _has_base() -> bool:
    if verify_worker.design_base(data.store()) is not None:
        return True
    print("This machine holds no verify base pin, so it cannot run a design review. "
          "Run this job on a verify worker.", flush=True)
    return False


def run_one(n: int) -> int:
    if not _has_base():
        return 1
    pr = data.store().load_pr(n)
    if pr is None or not design_review.reviewable(pr):
        print(f"PR #{n}: skipped (closed, a draft, stacked, not threat-scanned clear at its "
              "head, or routed to a close)", flush=True)
        return 0
    print(f"PR #{n}: reviewing", flush=True)
    outcome = verify_worker.run_design(n, trigger=ONE_TRIGGER, force=True)
    if outcome is False:
        print(f"PR #{n}: failed (see the design-review:run ledger row)", flush=True)
        return 1
    if outcome is None:
        print(f"PR #{n}: skipped (head moved, claimed elsewhere, or no diff)", flush=True)
        return 0
    entry = data.store().load_pr(n)
    findings = (entry.review_entry("design") or {}).get("findings", []) if entry else []
    print(f"PR #{n}: done, {len(findings)} finding(s)", flush=True)
    return 0


def run(limit: int) -> int:
    if not _has_base():
        return 1
    queue = candidates()
    print(f"{len(queue)} PR heads have no design review; reviewing up to {limit}.", flush=True)
    landed = failures = 0
    for n in queue:
        if landed >= limit:
            break
        print(f"PR #{n}: reviewing", flush=True)
        outcome = verify_worker.run_design(n, trigger=TRIGGER)
        if outcome is True:
            landed += 1
            failures = 0
            entry = data.store().load_pr(n)
            findings = (entry.review_entry("design") or {}).get("findings", []) if entry else []
            print(f"PR #{n}: done, {len(findings)} finding(s) [{landed}/{limit}]", flush=True)
        elif outcome is False:
            failures += 1
            print(f"PR #{n}: failed (see the design-review:run ledger row)", flush=True)
            if failures >= MAX_FAILURES_IN_A_ROW:
                print(f"Stopping after {failures} failures in a row.", flush=True)
                return 1
        else:
            print(f"PR #{n}: skipped (head moved, claimed elsewhere, or no diff)", flush=True)
    print(f"Done: {landed} design review(s) landed.", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--limit", type=int, default=20)
    group.add_argument("--pr", type=int)
    args = ap.parse_args()
    return run_one(args.pr) if args.pr is not None else run(args.limit)


if __name__ == "__main__":
    sys.exit(main())
