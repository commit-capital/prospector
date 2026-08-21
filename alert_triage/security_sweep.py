"""One sweep over the security families: alert ingest, alert find-fixed,
advisory ingest, advisory find-fixed, in that order in one process, so the
Control tab has one button and one progress stream. A step that fails is
reported and the sweep continues; the exit code is 1 if any step failed.

  uv run python alert_triage/security_sweep.py [--limit N] [--store DIR]
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from alert_triage import advisory_find_fixed
from alert_triage import advisory_ingest
from alert_triage import alert_ingest
from alert_triage import find_fixed

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
    failed = 0
    for name, run, takes_limit in STEPS:
        print(f"▶ {name}", flush=True)
        step_argv = (["--limit", str(args.limit)] if takes_limit else []) + store_args
        try:
            rc = run(step_argv)
            if isinstance(rc, int) and rc != 0:
                failed += 1
                print(f"  ! {name} exited {rc}", flush=True)
        except SystemExit as e:
            if e.code not in (0, None):
                failed += 1
                print(f"  ! {name} failed: {e.code}", flush=True)
        except Exception as e:
            failed += 1
            print(f"  ! {name} failed: {e}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
