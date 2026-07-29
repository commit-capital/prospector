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

import time

from pipeline import diffpaths, gates, profile, verify_driver


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
        paths = diffpaths.changed_paths(patch.read_text())
        if not paths:
            result["refused"] = "the PR diff is empty or unparsable"
            return result
        if gates.deps_touched(paths):
            result["refused"] = (
                "the PR changes dependency manifests — PR-controlled "
                "dependencies are never installed in the sandbox, so its "
                "compile result would be meaningless; verify by hand")
            return result
        sha = verify_driver.resolve_base_sha()
        result["base_sha"] = sha
        tag = verify_driver.base_image_tag(sha, 1)
        if not verify_driver.image_exists(tag):
            verify_driver.build_base_image(sha, tier=1)
        exit_code, tail = verify_driver.run_phase(
            "compile", tag, patch=patch, tier=1, test_cmd=cmd,
            base_sha=sha, head_sha=head_sha)
        result["exit"] = exit_code
        if exit_code == gates.SENTINEL_TEST_FAIL:
            result["error_excerpt"] = verify_driver.error_excerpt(tail)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        result["duration_s"] = round(time.monotonic() - t0, 1)
    return result
