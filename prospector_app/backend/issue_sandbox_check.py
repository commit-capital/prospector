"""The one host command an issue-fix lane agent may run: the project's typecheck
or its test runner over named files, inside the verify sandbox over the machine's
pinned base plus the stage's frozen reproduction tests plus the agent's
uncommitted edits. The base, the frozen tests, the records file and the run cap
arrive in PROSPECTOR_ISSUE_CHECK_* set by the worker, never on argv, so the agent
chooses only the lane and the files. The host runs nothing of the agent's own.

Invoked through prospector_app/agent/issue-sandbox-check, which the lane agent's
allowlist names. Every run appends a record for the worker to collect; past the
stage's run cap the tool refuses without running.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from issue_triage import lane_check
from pipeline import check_records, gates, prove
from prospector_app.backend import sandbox_check


def _runs_so_far(records: Path) -> int:
    """How many runs this stage has recorded, read without consuming the file:
    the worker collects it once the agent returns."""
    try:
        text = records.read_text()
    except FileNotFoundError:
        return 0
    count = 0
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            count += 1
    return count


def render(rec: dict) -> str:
    """The run as the agent reads it: a refusal, else the sandbox's own output."""
    if rec.get("refused"):
        return f"check refused: {rec['refused']}"
    return str(rec.get("output_tail", ""))[:4000]


def main(argv: list[str]) -> int:
    env = os.environ
    try:
        base = lane_check.base_from_env(env)
        issue = int(env["PROSPECTOR_ISSUE_CHECK_ISSUE"])
        worktree = env["PROSPECTOR_ISSUE_CHECK_WORKTREE"]
        records = Path(env["PROSPECTOR_ISSUE_CHECK_RECORDS"])
        max_runs = int(env["PROSPECTOR_ISSUE_CHECK_MAX_RUNS"])
    except (KeyError, ValueError):
        print("issue-sandbox-check: not running under the issue-fix lane "
              "(PROSPECTOR_ISSUE_CHECK_* unset)", file=sys.stderr)
        return 2
    cmd, why = sandbox_check.lane_command(argv)
    if cmd is None:
        print(f"issue-sandbox-check: {why}", file=sys.stderr)
        return 2
    if _runs_so_far(records) >= max_runs:
        print(f"check refused: this stage has used its {max_runs} sandbox runs")
        return 1
    test_patch = env.get("PROSPECTOR_ISSUE_CHECK_TEST_PATCH")
    label = f"issue-{issue}-check"
    try:
        patch = prove.compose(label, Path(test_patch) if test_patch else None,
                              sandbox_check.authored_patch(worktree))
        rec = prove.run_command(base, patch, cmd, label=label,
                                phase="compile" if argv == ["typecheck"] else "green")
    except (ValueError, subprocess.SubprocessError, OSError) as e:
        rec = {"cmd": cmd, "refused": str(e)}
    check_records.append(records, sandbox_check.check_record(argv, rec),
                         tool="issue-sandbox-check")
    print(render(rec))
    return 0 if rec.get("exit") == gates.SENTINEL_PASS else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
