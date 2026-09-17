"""Host-observed proof on this machine's pinned base: a test patch that fails
(red), a test-plus-fix patch that passes (green), and a command over a patched
tree. Every run uses the image the verify pin names, so a caller never builds
one. The exits are the verdict; the captured output is evidence."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from pipeline import diffpaths, gates, verify_driver
from pipeline.verify_driver import SCRATCH

if TYPE_CHECKING:
    from pipeline.store import Store

logger = logging.getLogger(__name__)

# How much of an unexpected exception's own text a record keeps: enough to
# name the cause from the record alone.
ERROR_CHARS = 1500


class NoBase(RuntimeError):
    """This machine has no usable pinned base: no pin, no Docker daemon, no
    image, or no clone. The machine's condition, never a verdict."""


@dataclass(frozen=True)
class PinnedBase:
    sha: str
    tier: int
    image: str
    clone: Path


def pinned(store: Store) -> PinnedBase:
    """The base this machine proves against: the verify pin's SHA and tier plus
    the image and clone they name, all present on local disk."""
    try:
        sha, tier = verify_driver._pin(store)
    except RuntimeError as e:
        raise NoBase(str(e)) from e
    image = verify_driver.base_image_tag(sha, tier)
    clone = verify_driver.base_clone_dir(sha)
    if not verify_driver.daemon_available():
        raise NoBase("the Docker daemon is not answering")
    if not verify_driver.image_exists(image):
        raise NoBase(f"the pinned base image {image} is not on this machine")
    if not clone.is_dir():
        raise NoBase(f"the pinned base clone {clone} is not on this machine")
    return PinnedBase(sha=sha, tier=tier, image=image, clone=clone)


def compose(label: str, *parts: Path | str | None) -> Path:
    """One patch file holding `parts` in order, under the verify scratch so the
    sandbox can mount it. Parts must touch disjoint paths: the sandbox applies
    the file in one `git apply`."""
    texts = [p.read_text() if isinstance(p, Path) else p for p in parts if p]
    seen: set[str] = set()
    for text in texts:
        paths = set(diffpaths.changed_paths(text))
        if seen & paths:
            raise ValueError(f"patch parts share paths: {sorted(seen & paths)}")
        seen |= paths
    out = SCRATCH / "issue-fix"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{label}.{time.monotonic_ns()}.patch"
    path.write_text("".join(t if t.endswith("\n") else t + "\n" for t in texts))
    return path


def run_command(base: PinnedBase, patch: Path, cmd: str, *,
                phase: Literal["green", "compile"], label: str) -> dict:
    """The sandbox record for `cmd` run over `base` with `patch` applied.
    Fail-safe shape, fail-closed content: every failure — a refusal, a docker
    error, a base that cannot pass the command itself — lands in the record;
    nothing raises into the caller."""
    t0 = time.monotonic()
    result: dict = {"cmd": cmd, "label": label, "base_sha": base.sha,
                    "output_tail": ""}
    try:
        _command_over(result, base, patch, cmd, phase, label)
    except Exception as e:
        logger.error("proof command failed unexpectedly",
                     exc_info=(type(e), e, e.__traceback__))
        result["error"] = f"{type(e).__name__}: {str(e)[-ERROR_CHARS:]}"
    finally:
        result["duration_s"] = round(time.monotonic() - t0, 1)
    return result


def _command_over(result: dict, base: PinnedBase, patch: Path, cmd: str,
                  phase: Literal["green", "compile"], label: str) -> None:
    """Run `cmd` in one `phase` container over `base` with `patch` applied,
    recording the outcome into `result`. Refusals (an empty patch, a dependency
    manifest change) land as `refused` and run nothing."""
    paths = diffpaths.changed_paths(patch.read_text())
    if not paths:
        result["refused"] = "the patch is empty or unparsable"
        return
    if gates.deps_touched(paths):
        result["refused"] = (
            "the patch changes dependency manifests — patch-controlled "
            "dependencies are never installed in the sandbox, so its "
            "result would be meaningless; verify by hand")
        return
    # head_sha is an identifying stamp the sandbox exports into the container
    # and nothing reads back, so a proof run puts the caller's label there.
    exit_code, tail = verify_driver.run_phase(
        phase, base.image, patch=patch, tier=base.tier, test_cmd=cmd,
        base_sha=base.sha, head_sha=label)
    result["exit"] = exit_code
    result["output_tail"] = tail
    excerpt = verify_driver.error_excerpt(tail)
    if exit_code == gates.SENTINEL_TEST_FAIL:
        result["error_excerpt"] = excerpt
        # The sandbox runs a pristine base for the compile and build phases
        # only, so a failing green leg reads as the patch's own verdict.
        if phase == "compile":
            _record_base_fault(result, base, cmd)
    elif exit_code == gates.SENTINEL_PATCH_CONFLICT:
        result["error_excerpt"] = excerpt
    elif exit_code == gates.SENTINEL_PATCH_UNREADABLE:
        result["error"] = ("the sandbox could not apply the patch for a reason that "
                           "is not the patch's" + (f": {excerpt}" if excerpt else ""))
    elif exit_code not in (gates.SENTINEL_PASS, gates.SENTINEL_PROBE_FAIL):
        result["error"] = (f"the sandbox phase exited {exit_code} before the command "
                           f"concluded" + (f": {excerpt}" if excerpt else ""))


def _record_base_fault(result: dict, base: PinnedBase, cmd: str) -> None:
    """Re-run `cmd` over the pristine base and, when it fails there too, record
    the failure as the lane's own fault."""
    base_failure = verify_driver.base_command_failure(
        base.image, cmd, lambda: verify_driver.run_phase(
            "compile", base.image, tier=base.tier, test_cmd=cmd,
            base_sha=base.sha, head_sha="pristine", pristine=True))
    if base_failure is not None:
        result["error"] = gates.base_fault_text(
            "compile", f"at {base.sha[:12]}: {base_failure}")
        result["error_kind"] = "base-compile"


class Legs(TypedDict):
    exit: int | None
    exit_confirm: int | None
    output_tail: str
    duration_s: float


def red_legs(base: PinnedBase, *, patch: Path, test_cmd: str, label: str) -> Legs:
    """Run the reproduction test over `base` with `patch` applied: the proof is
    a failing first leg confirmed by a second, fresh container."""
    return _legs(base, "red", gates.SENTINEL_TEST_FAIL, patch=patch,
                 test_cmd=test_cmd, label=label)


def green_legs(base: PinnedBase, *, patch: Path, test_cmd: str, label: str) -> Legs:
    """Run the reproduction test over `base` with the test-plus-fix patch
    applied: the proof is a passing first leg confirmed by a second, fresh
    container."""
    return _legs(base, "green", gates.SENTINEL_PASS, patch=patch,
                 test_cmd=test_cmd, label=label)


def _legs(base: PinnedBase, phase: Literal["red", "green"], want: int, *,
          patch: Path, test_cmd: str, label: str) -> Legs:
    """Two legs of `phase`, the second run only when the first exits `want`. A
    probe failure raises: no code runs on a sandbox whose isolation is
    unproven, so such a leg refuses and emits no verdict."""
    t0 = time.monotonic()

    def leg() -> tuple[int, str]:
        exit_code, tail = verify_driver.run_phase(
            phase, base.image, patch=patch, tier=base.tier, test_cmd=test_cmd,
            base_sha=base.sha, head_sha=label)
        if exit_code == gates.SENTINEL_PROBE_FAIL:
            raise verify_driver.ProbeFailure(
                f"sandbox isolation could not be proven ({label}, phase {phase})")
        return exit_code, tail

    first, tail = leg()
    confirm = leg()[0] if first == want else None
    return Legs(exit=first, exit_confirm=confirm, output_tail=tail,
                duration_s=round(time.monotonic() - t0, 1))
