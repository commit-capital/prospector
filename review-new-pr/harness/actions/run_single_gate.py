#!/usr/bin/env python3
"""run_single_gate.py — GitHub Actions entry point for a single gate.

Builds a PRContext from inputs available inside a GitHub Actions runner
(git diff, gh api output) and runs ONE gate, writing the result to disk.

Intentionally NOT a clone of run_harness.py — the runtime data sources are
different (no prs-cache.jsonl, no diffs/<n>.json — we fetch live).
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARNESS_DIR))

from gates._common import PRContext, default_branch  # noqa: E402
from gates import secret_scan, dep_diff, pr_template_check  # noqa: E402

GATES = {
    "secret_scan": secret_scan.run,
    "dep_diff": dep_diff.run,
    "pr_template_check": pr_template_check.run,
}


def collect_files_from_git_diff(base_sha: str, head_sha: str) -> list[dict]:
    """Build the `files` list from `git diff base..head` output.

    Returns the same shape as the gh API's /pulls/N/files endpoint:
    [{filename, status, additions, deletions, patch}, ...]
    """
    # Get file list with stat
    name_status = subprocess.run(
        ["git", "diff", "--name-status", f"{base_sha}..{head_sha}"],
        check=True, capture_output=True, text=True,
    ).stdout

    files: list[dict] = []
    for line in name_status.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        status_code = parts[0][0]  # A/M/D/R...
        status_map = {"A": "added", "M": "modified", "D": "removed", "R": "renamed"}
        status = status_map.get(status_code, "modified")
        filename = parts[-1]

        # Per-file diff (patch)
        try:
            patch = subprocess.run(
                ["git", "diff", "--unified=3", f"{base_sha}..{head_sha}", "--", filename],
                check=True, capture_output=True, text=True,
            ).stdout
        except subprocess.CalledProcessError:
            patch = ""

        # Quick add/del count
        adds = sum(1 for line in patch.split("\n") if line.startswith("+") and not line.startswith("+++"))
        dels = sum(1 for line in patch.split("\n") if line.startswith("-") and not line.startswith("---"))

        files.append({
            "filename": filename,
            "status": status,
            "additions": adds,
            "deletions": dels,
            "patch": patch,
        })
    return files


def build_ctx_from_git(args) -> PRContext:
    files = collect_files_from_git_diff(args.base, args.head)
    return PRContext(
        number=int(args.pr),
        title="",
        body="",
        author="",
        head_sha=args.head,
        base_ref=default_branch(),
        additions=sum(f["additions"] for f in files),
        deletions=sum(f["deletions"] for f in files),
        changed_files=len(files),
        files=files,
    )


def build_ctx_from_meta(args) -> PRContext:
    """For pr_template_check: we need the PR body, fetched via gh api into meta json."""
    meta = json.loads(Path(args.meta).read_text())
    return PRContext(
        number=meta["number"],
        title=meta.get("title") or "",
        body=meta.get("body") or "",
        author=meta.get("author") or "",
        head_sha=meta.get("head") or "",
        base_ref=default_branch(),
        additions=None,
        deletions=None,
        changed_files=None,
        files=[],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", required=True, choices=GATES.keys())
    parser.add_argument("--out", required=True)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--base", help="base commit SHA")
    parser.add_argument("--head", help="head commit SHA")
    parser.add_argument("--meta", help="path to PR meta JSON (for body-based gates)")
    args = parser.parse_args()

    # Build context appropriate to the gate
    if args.gate in {"secret_scan", "dep_diff"}:
        if not (args.base and args.head and args.pr):
            parser.error(f"--gate {args.gate} requires --base, --head, --pr")
        ctx = build_ctx_from_git(args)
    elif args.gate == "pr_template_check":
        if not args.meta:
            parser.error("--gate pr_template_check requires --meta")
        ctx = build_ctx_from_meta(args)
    else:
        parser.error(f"Unknown gate {args.gate}")

    result = GATES[args.gate](ctx)
    Path(args.out).write_text(json.dumps({
        "gate": args.gate,
        "pr": ctx.number,
        "head_sha": ctx.head_sha,
        "result": result,
    }, indent=2))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
