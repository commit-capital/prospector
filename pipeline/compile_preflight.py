"""The merge-time compile preflight: run the profile's compile command over
(current default-branch HEAD + the PR's diff) in the isolated sandbox and
record the container exit as the verdict.

This module only produces the record; the policy over it is
gates.compile_preflight_gate, and the executor runs both immediately before a
live merge fires. The base is resolved live per run and its tier-1 image built
ephemerally (reused while the default branch stays put) — the stored verify
pin plays no part, so the compile always measures the PR against the branch it
is about to land on. A PR that changes dependency manifests is refused:
PR-controlled dependencies are never installed in the sandbox, so its compile
result would be meaningless.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from pipeline import diffpaths, gates, profile, verify_driver
from pipeline.store import Store

logger = logging.getLogger(__name__)


def _record_unexpected_error(result: dict, error: Exception) -> None:
    logger.error("compile preflight failed unexpectedly",
                 exc_info=(type(error), error, error.__traceback__))
    result["error"] = "compile preflight failed unexpectedly; see server logs"


def run_for_patch(pr: int, head_sha: str, patch: Path) -> dict | None:
    """The compile-preflight record for `pr` with `patch` applied over current
    default-branch HEAD, or None when the active profile configures no
    verify.compile_cmd. Takes the patch rather than fetching it, so a change
    that exists only in a local worktree — an autofix authored but not yet
    pushed — is measured before it reaches the contributor's branch.

    Fail-safe shape, fail-closed content: every failure — a refusal, an
    unresolvable base, a docker or build error — lands in the record for
    gates.compile_preflight_gate to block on; nothing raises into the caller."""
    cmd = profile.active().verify.compile_cmd
    if cmd is None:
        return None
    return run_command_for_patch(pr, head_sha, patch, cmd)


def run_command_for_patch(pr: int, head_sha: str, patch: Path, cmd: str) -> dict:
    """The sandbox record for `cmd` run over current default-branch HEAD with
    `patch` applied — the compile preflight's mechanics under a caller-chosen
    command, which is how the fix author's sandbox check runs the profile's
    test runner over named test files. Fail-safe shape, fail-closed content:
    every failure lands in the record; nothing raises into the caller."""
    t0 = time.monotonic()
    result: dict = {"cmd": cmd, "pr": pr, "head_sha": head_sha}
    try:
        _compile_over(result, patch, cmd, head_sha)
    except Exception as e:
        _record_unexpected_error(result, e)
    finally:
        result["duration_s"] = round(time.monotonic() - t0, 1)
    return result


def run_for_merge(pr: int, head_sha: str) -> dict | None:
    """The compile-preflight record for a live merge of `pr` at `head_sha`, or
    None when the active profile configures no verify.compile_cmd (the
    deployment requires no compile preflight). Fail-safe shape, fail-closed
    content: every failure — a refusal, an unresolvable base, a docker or
    build error — lands in the record for gates.compile_preflight_gate to
    block on; nothing raises into the merge path."""
    cmd = profile.active().verify.compile_cmd
    if cmd is None:
        return None
    t0 = time.monotonic()
    result: dict = {"cmd": cmd, "pr": pr, "head_sha": head_sha}
    try:
        if not head_sha:
            result["refused"] = "the PR has no recorded head SHA"
            return result
        patch = verify_driver.fetch_patch(pr, head_sha)
        _compile_over(result, patch, cmd, head_sha)
    except Exception as e:
        _record_unexpected_error(result, e)
    finally:
        result["duration_s"] = round(time.monotonic() - t0, 1)
    return result


def _compile_over(result: dict, patch: Path, cmd: str, head_sha: str) -> None:
    """Build the tier-1 base image for current default-branch HEAD and run `cmd`
    over it with `patch` applied, recording the outcome into `result`. Refusals
    (an empty diff, a dependency-manifest change) land as `refused` and run
    nothing."""
    paths = diffpaths.changed_paths(patch.read_text())
    if not paths:
        result["refused"] = "the PR diff is empty or unparsable"
        return
    if gates.deps_touched(paths):
        result["refused"] = (
            "the PR changes dependency manifests — PR-controlled "
            "dependencies are never installed in the sandbox, so its "
            "compile result would be meaningless; verify by hand")
        return
    sha = verify_driver.resolve_base_sha()
    result["base_sha"] = sha
    tag = verify_driver.base_image_tag(sha, 1)
    if not verify_driver.image_exists(tag):
        try:
            pin = verify_driver.local_pin(Store()).get("base_sha")
            verify_driver.collect_garbage(str(pin) if pin else None)
        except Exception:
            logger.warning("compile preflight could not collect verify garbage", exc_info=True)
        verify_driver.build_base_image(sha, tier=1)
    exit_code, tail = verify_driver.run_phase(
        "compile", tag, patch=patch, tier=1, test_cmd=cmd,
        base_sha=sha, head_sha=head_sha)
    result["exit"] = exit_code
    if exit_code == gates.SENTINEL_TEST_FAIL:
        result["error_excerpt"] = verify_driver.error_excerpt(tail)
