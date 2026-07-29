#!/usr/bin/env python3
"""run_harness.py — local driver for Layer 1 gates.

Runs the static gates against either a single PR or the full cached PR set.

Usage:
  # Single PR
  python3 run_harness.py --pr 4318

  # Batch over all cached PRs, write verdicts to harness/v1/verdicts/
  python3 run_harness.py --batch

  # Re-run a single gate (useful when iterating on detection logic)
  python3 run_harness.py --pr 4318 --gate secret_scan
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
ROOT_DIR = HARNESS_DIR.parent.parent  # repo root
sys.path.insert(0, str(HARNESS_DIR))

from gates._common import PRContext  # noqa: E402
from gates import secret_scan, dep_diff, pr_template_check  # noqa: E402


GATES = {
    "secret_scan": secret_scan.run,
    "dep_diff": dep_diff.run,
    "pr_template_check": pr_template_check.run,
}

PRS_CACHE = ROOT_DIR / "prs-cache.jsonl"
DIFFS_DIR = ROOT_DIR / "diffs"
VERDICTS_DIR = HARNESS_DIR / "verdicts"


def derive_overall(gate_results: dict[str, dict]) -> str:
    """Roll up per-gate verdicts into a single overall verdict.

    Rules:
    - Any `fail` with severity high|critical -> auto_reject
    - Any `fail` with lower severity OR any `uncertain` -> needs_human_review
    - All pass -> pass
    """
    has_fail_high = False
    has_fail_low = False
    has_uncertain = False
    for gate_name, result in gate_results.items():
        v = result.get("verdict")
        sev = result.get("severity")
        if v == "fail" and sev in {"high", "critical"}:
            has_fail_high = True
        elif v == "fail":
            has_fail_low = True
        elif v == "uncertain":
            has_uncertain = True
    if has_fail_high:
        return "auto_reject"
    if has_fail_low or has_uncertain:
        return "needs_human_review"
    return "pass"


def build_machine_summary(gate_results: dict[str, dict], overall: str) -> str:
    """Single-line summary humans skim before opening the verdict JSON."""
    parts = []
    for gate, result in gate_results.items():
        v = result["verdict"]
        sev = result.get("severity") or ""
        sev_tag = f"/{sev}" if sev else ""
        parts.append(f"{gate}={v}{sev_tag}")
    return f"overall={overall} | " + " | ".join(parts)


def run_one(pr_num: int, gate_filter: str | None = None) -> dict:
    """Run gates against a single PR; return the full verdict dict."""
    ctx = PRContext.from_caches(pr_num, str(PRS_CACHE), str(DIFFS_DIR))
    gate_results: dict[str, dict] = {}
    for name, fn in GATES.items():
        if gate_filter and name != gate_filter:
            continue
        gate_results[name] = fn(ctx)

    overall = derive_overall(gate_results)
    return {
        "pr": ctx.number,
        "head_sha": ctx.head_sha,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "title": ctx.title,
        "author": ctx.author,
        "gates": gate_results,
        "overall": overall,
        "machine_summary": build_machine_summary(gate_results, overall),
    }


def run_batch() -> dict:
    """Run all cached PRs; write per-PR verdict JSONs and aggregate stats."""
    VERDICTS_DIR.mkdir(exist_ok=True)
    pr_numbers: list[int] = []
    with open(PRS_CACHE) as f:
        for line in f:
            pr_numbers.append(json.loads(line)["number"])

    stats = {
        "pass": 0,
        "needs_human_review": 0,
        "auto_reject": 0,
        "errors": 0,
        "total": len(pr_numbers),
    }
    by_gate_verdict: dict[str, dict[str, int]] = {
        g: {"pass": 0, "fail": 0, "uncertain": 0, "error": 0}
        for g in GATES
    }

    for i, n in enumerate(pr_numbers):
        try:
            verdict = run_one(n)
            (VERDICTS_DIR / f"{n}.json").write_text(json.dumps(verdict, indent=2))
            stats[verdict["overall"]] += 1
            for gate, result in verdict["gates"].items():
                by_gate_verdict[gate][result["verdict"]] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"  !! PR #{n}: {e}", file=sys.stderr)

        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{stats['total']}] processed")

    aggregate = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "overall_distribution": stats,
        "by_gate_verdict": by_gate_verdict,
    }
    (VERDICTS_DIR / "_aggregate.json").write_text(json.dumps(aggregate, indent=2))
    return aggregate


def main() -> int:
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int, help="PR number to evaluate")
    g.add_argument("--batch", action="store_true", help="Run over all cached PRs")
    parser.add_argument("--gate", choices=GATES.keys(), help="Run only one gate")
    args = parser.parse_args()

    if args.batch:
        result = run_batch()
        print(json.dumps(result, indent=2))
    else:
        verdict = run_one(args.pr, gate_filter=args.gate)
        print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
