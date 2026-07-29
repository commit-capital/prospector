#!/usr/bin/env python3
"""reduce_verdicts.py — combine per-gate verdict artifacts into one verdict.

Runs in the Actions reducer job that does NOT check out contributor code.
Inputs: directory of per-gate verdict JSONs from the downloaded artifacts.
Output: single verdict.json in the harness verdict schema.
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def derive_overall(gate_results: dict[str, dict]) -> str:
    has_fail_high = False
    has_fail_low = False
    has_uncertain = False
    for r in gate_results.values():
        v = r.get("verdict")
        sev = r.get("severity")
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


def build_summary(gate_results: dict[str, dict], overall: str) -> str:
    parts = []
    for name, r in gate_results.items():
        v = r.get("verdict", "?")
        sev = r.get("severity") or ""
        sev_tag = f"/{sev}" if sev else ""
        parts.append(f"{name}={v}{sev_tag}")
    return f"overall={overall} | " + " | ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="indir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args()

    indir = Path(args.indir)
    gate_results: dict[str, dict] = {}

    # Each per-gate artifact directory contains a single verdict-<gate>.json file
    for artifact_dir in indir.iterdir():
        if not artifact_dir.is_dir():
            continue
        for f in artifact_dir.glob("verdict-*.json"):
            try:
                payload = json.loads(f.read_text())
            except Exception as e:
                print(f"  warn: failed to parse {f}: {e}", file=sys.stderr)
                continue
            gate_results[payload["gate"]] = payload["result"]

    if not gate_results:
        print("!! No gate verdicts found — gates may have failed before producing artifacts", file=sys.stderr)
        gate_results["_missing"] = {
            "verdict": "uncertain",
            "severity": "high",
            "evidence": "No gate produced a verdict artifact. Pipeline error.",
        }

    overall = derive_overall(gate_results)
    out = {
        "pr": args.pr,
        "head_sha": args.head_sha,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "gates": gate_results,
        "overall": overall,
        "machine_summary": build_summary(gate_results, overall),
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
