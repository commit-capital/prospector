"""The one command the fix-authoring agent may run to exercise its change: the
project's typecheck, or its test runner over named test files, inside the
verify sandbox over current default-branch HEAD with the pull request's diff
and the agent's current worktree edits applied. The host runs nothing of the
contributor's; the agent chooses only the lane and the test files.

Invoked through prospector_app/agent/sandbox-check, which the authoring agent's
allowlist names. The pull request, its head, the worktree and the PR's diff
file arrive in PROSPECTOR_CHECK_* environment variables set by author_fix,
never on argv, so the agent cannot point the check at another tree.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from pipeline import compile_preflight, diffpaths, gates, profile, settings

USAGE = ("usage: sandbox-check typecheck | "
         "sandbox-check test <repo-relative test file>...")


def lane_command(argv: list[str]) -> tuple[str | None, str | None]:
    """The sandbox command for the agent's argv, or (None, why) when the argv
    names no lane this tool runs. `typecheck` is the profile's compile command;
    `test` is the profile's fixed runner over the named files, each of which
    must be a repo-relative test path."""
    if argv == ["typecheck"]:
        cmd = profile.active().verify.compile_cmd
        if cmd is None:
            return None, "this deployment configures no typecheck command"
        return cmd, None
    if len(argv) > 1 and argv[0] == "test":
        files = argv[1:]
        for f in files:
            if (f.startswith("/") or ".." in Path(f).parts
                    or not diffpaths.is_test_path(f)):
                return None, f"not a repo-relative test file: {f}"
        v = profile.active().verify
        return " ".join([*v.test_runner, *(shlex.quote(f) for f in files),
                         *v.test_flags]), None
    return None, USAGE


def authored_patch(worktree: str) -> str:
    """The agent's uncommitted edits as a diff against the worktree's HEAD; new
    files are marked intent-to-add so they appear. Read-only: nothing is
    staged for commit."""
    subprocess.run(["git", "-C", worktree, "add", "-N", "."], check=True,
                   capture_output=True, text=True, timeout=120)
    r = subprocess.run(["git", "-C", worktree, "diff", "HEAD"], check=True,
                       capture_output=True, text=True, timeout=120)
    return r.stdout


def combined_patch(pr: int, pr_patch: Path, authored: str) -> Path:
    """One patch the sandbox applies onto the default branch: the pull request's
    own diff with the agent's edits appended, so the check measures the tree the
    edits were written against."""
    scratch = settings.verify_scratch() / "autofix"
    scratch.mkdir(parents=True, exist_ok=True)
    text = pr_patch.read_text()
    if not text.endswith("\n"):
        text += "\n"
    if authored and not authored.endswith("\n"):
        authored += "\n"
    p = scratch / f"pr-{pr}.check.patch"
    p.write_text(text + authored)
    return p


def render(rec: dict) -> str:
    """The record as the agent reads it: the verdict first, then the evidence."""
    if rec.get("refused"):
        return f"check refused: {rec['refused']}"
    if rec.get("error"):
        return f"check could not run: {rec['error']}"
    code = rec.get("exit")
    head = f"{rec.get('cmd')}\nexit {code} after {rec.get('duration_s')}s"
    if code == gates.SENTINEL_PASS:
        return head + "\nPASS"
    if code == gates.SENTINEL_PATCH_CONFLICT:
        return head + ("\nFAIL: the pull request plus your edits do not apply onto "
                       "the current default branch")
    return head + "\nFAIL\n" + str(rec.get("error_excerpt") or "")


def main(argv: list[str]) -> int:
    env = os.environ
    try:
        pr = int(env["PROSPECTOR_CHECK_PR"])
        head = env["PROSPECTOR_CHECK_HEAD"]
        worktree = env["PROSPECTOR_CHECK_WORKTREE"]
        pr_patch = Path(env["PROSPECTOR_CHECK_PR_PATCH"])
    except (KeyError, ValueError):
        print("sandbox-check: not running under the fix author "
              "(PROSPECTOR_CHECK_* unset)", file=sys.stderr)
        return 2
    cmd, why = lane_command(argv)
    if cmd is None:
        print(f"sandbox-check: {why}", file=sys.stderr)
        return 2
    try:
        authored = authored_patch(worktree)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"sandbox-check: reading the worktree's edits failed: {e}", file=sys.stderr)
        return 2
    patch = combined_patch(pr, pr_patch, authored)
    rec = compile_preflight.run_command_for_patch(pr, head, patch, cmd)
    print(render(rec))
    return 0 if rec.get("exit") == gates.SENTINEL_PASS else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
