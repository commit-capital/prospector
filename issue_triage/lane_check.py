"""The environment the issue-fix lane hands its check tool, and the pinned base
read back from it. The worker composes the environment for one stage's agent;
the tool (prospector_app.backend.issue_sandbox_check) reads it back, so the tree
the agent measures and the run cap it obeys come from the worker, never the
agent's argv.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from pipeline import prove

REPO_ROOT = Path(__file__).resolve().parents[1]

# The one host command a lane agent may run; its allowlist names this path.
TOOL = str(REPO_ROOT / "prospector_app" / "agent" / "issue-sandbox-check")

# Sandbox runs one stage's agent may make before the tool refuses.
MAX_RUNS = 8


def records_path(workdir: Path, stage: str) -> Path:
    """Where one stage's runs are recorded: inside the run's own `workdir`, so
    two runs of one issue never share a record or a run cap."""
    return workdir / f"{stage}.checks.jsonl"


def check_env(*, issue: int, base: prove.PinnedBase, worktree: Path, records: Path,
              test_patch: Path | None, pre_patch: Path | None = None) -> dict[str, str]:
    """The PROSPECTOR_ISSUE_CHECK_* environment the tool reads: the issue, the
    pinned base, the agent's worktree, the records file, the run cap, the frozen
    reproduction tests when the stage has them, and the patch carrying the base
    to the tree the agent's clone was made from when the caller named one — the
    agent's own diff is against that tree, so the sandbox needs it to compose the
    same tree. PROSPECTOR_PYTHON names the interpreter the shim execs."""
    env = {
        "PROSPECTOR_ISSUE_CHECK_ISSUE": str(issue),
        "PROSPECTOR_ISSUE_CHECK_BASE_SHA": base.sha,
        "PROSPECTOR_ISSUE_CHECK_TIER": str(base.tier),
        "PROSPECTOR_ISSUE_CHECK_IMAGE": base.image,
        "PROSPECTOR_ISSUE_CHECK_CLONE": str(base.clone),
        "PROSPECTOR_ISSUE_CHECK_WORKTREE": str(worktree),
        "PROSPECTOR_ISSUE_CHECK_RECORDS": str(records),
        "PROSPECTOR_ISSUE_CHECK_MAX_RUNS": str(MAX_RUNS),
        "PROSPECTOR_PYTHON": sys.executable,
    }
    if test_patch is not None:
        env["PROSPECTOR_ISSUE_CHECK_TEST_PATCH"] = str(test_patch)
    if pre_patch is not None:
        env["PROSPECTOR_ISSUE_CHECK_PRE_PATCH"] = str(pre_patch)
    return env


def base_from_env(env: Mapping[str, str]) -> prove.PinnedBase:
    """The pinned base check_env wrote, read back for prove.run_command."""
    return prove.PinnedBase(
        sha=env["PROSPECTOR_ISSUE_CHECK_BASE_SHA"],
        tier=int(env["PROSPECTOR_ISSUE_CHECK_TIER"]),
        image=env["PROSPECTOR_ISSUE_CHECK_IMAGE"],
        clone=Path(env["PROSPECTOR_ISSUE_CHECK_CLONE"]))
