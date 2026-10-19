#!/usr/bin/env python3
"""Ask the operator before opening a PR that grows AGENTS.md past a few lines."""

from __future__ import annotations

import json
import re
import subprocess
import sys


PR_CREATE = re.compile(r"(?:^|[\s;&|()])(?:[^\s;&|()]*/)?gh\s+pr\s+create\b")
MAX_NET_GROWTH = 5


def git(cwd: str, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", cwd, *arguments], capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def net_growth(cwd: str) -> int:
    base = git(cwd, "merge-base", "HEAD", "origin/main") or git(
        cwd, "merge-base", "HEAD", "main"
    )
    if not base:
        return 0
    numstat = git(cwd, "diff", "--numstat", base, "HEAD", "--", "AGENTS.md")
    if not numstat:
        return 0
    added, deleted, _ = numstat.split("\t", 2)
    return int(added) - int(deleted)


def main() -> int:
    try:
        event = json.load(sys.stdin)
        command = event["tool_input"]["command"]
        cwd = event.get("cwd") or "."
    except (json.JSONDecodeError, KeyError, TypeError, OSError):
        return 0
    if not isinstance(command, str) or not PR_CREATE.search(command):
        return 0
    growth = net_growth(cwd)
    if growth <= MAX_NET_GROWTH:
        return 0
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    f"This branch adds {growth} net lines to AGENTS.md. It holds "
                    "invariants only; feature docs go in the module docstring, "
                    "docs/, or the PR description."
                ),
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
