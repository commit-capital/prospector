"""One sweep over the security families: alert ingest, alert find-fixed,
advisory ingest, advisory find-fixed, in that order in one process, so the
Control tab has one button and one progress stream. A step that fails is
reported and the sweep continues; the exit code is 1 if any step failed.

  uv run python alert_triage/security_sweep.py [--limit N] [--store DIR]
"""
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable

from alert_triage import advisory_find_fixed
from alert_triage import advisory_ingest
from alert_triage import alert_ingest
from alert_triage import find_fixed
from pipeline import progress

Step = tuple[str, Callable[[list[str] | None], int | None], bool]

# (name, entry point, takes --limit)
STEPS: list[Step] = [
    ("alert-ingest", alert_ingest.main, False),
    ("alert-find-fixed", find_fixed.main, True),
    ("advisory-ingest", advisory_ingest.main, False),
    ("advisory-find-fixed", advisory_find_fixed.main, True),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=12,
                    help="max records each find-fixed pass scans (default 12)")
    ap.add_argument("--store", default=None, help="store root override (tests/smoke)")
    args = ap.parse_args(argv)
    store_args = ["--store", args.store] if args.store else []
    failed: list[str] = []
    sweep_started = time.monotonic()
    for step, (name, run, takes_limit) in enumerate(STEPS, 1):
        print(f"▶ {name} (step {step} of {len(STEPS)})", flush=True)
        step_started = time.monotonic()
        step_argv = (["--limit", str(args.limit)] if takes_limit else []) + store_args
        problem: str | None = None
        try:
            rc = run(step_argv)
            if isinstance(rc, int) and rc != 0:
                problem = f"exited {rc}"
        except SystemExit as e:
            if e.code not in (0, None):
                problem = f"failed: {e.code}"
        except Exception as e:
            problem = f"failed: {e}"
        took = progress.duration(time.monotonic() - step_started)
        if problem is None:
            print(f"  ✓ {name} done in {took}", flush=True)
        else:
            failed.append(name)
            print(f"  ! {name} {problem} after {took}", flush=True)
    total = progress.duration(time.monotonic() - sweep_started)
    if failed:
        print(f"■ sweep finished in {total}; {len(failed)} of {len(STEPS)} steps "
              f"failed: {', '.join(failed)}", flush=True)
    else:
        print(f"■ sweep finished in {total}; all {len(STEPS)} steps succeeded", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
