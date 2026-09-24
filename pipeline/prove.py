"""Host-observed proof on this machine's pinned base: a test patch that fails
(red), a test-plus-fix patch that passes (green), a command over a patched
tree, and the full suite over a patched tree against the same tree's own
failing set (`suite_regress`). Every run uses the image the verify pin names,
so a caller never builds one. The exits are the verdict; the captured output is
evidence. A sandbox whose isolation probe fails lands in `run_command`'s record
as its exit and raises `ProbeFailure` out of the legs."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from pipeline import diffpaths, gates, verify_driver

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


def held(base_sha: str, tier: int) -> PinnedBase:
    """The base named by `base_sha` and `tier`, with the image and clone they
    derive present on local disk. `NoBase` when the daemon, the image, or the
    clone is absent; never builds."""
    image = verify_driver.base_image_tag(base_sha, tier)
    clone = verify_driver.base_clone_dir(base_sha)
    if not verify_driver.daemon_available():
        raise NoBase("the Docker daemon is not answering")
    if not verify_driver.image_exists(image):
        raise NoBase(f"the base image {image} is not on this machine")
    if not clone.is_dir():
        raise NoBase(f"the base clone {clone} is not on this machine")
    return PinnedBase(sha=base_sha, tier=tier, image=image, clone=clone)


# A label is host-chosen and becomes a file name, so it is held to plain
# file-name characters with no parent-directory segment.
_LABEL_RE = re.compile(r"[A-Za-z0-9._-]+")


def _check_label(label: str) -> None:
    if not _LABEL_RE.fullmatch(label) or ".." in label:
        raise ValueError(f"label is not a file name: {label!r}")


def compose(label: str, *parts: Path | str | None) -> Path:
    """One patch file holding `parts` in order, under the verify scratch so the
    sandbox can mount it. Named for `label` and the digest of the composed text,
    so one composition is one file and the directory is bounded by the distinct
    compositions on this machine. Parts must touch disjoint paths: the sandbox
    applies the file in one `git apply`."""
    _check_label(label)
    texts = [p.read_text() if isinstance(p, Path) else p for p in parts if p]
    seen: set[str] = set()
    for text in texts:
        paths = set(diffpaths.changed_paths(text))
        if seen & paths:
            raise ValueError(f"patch parts share paths: {sorted(seen & paths)}")
        seen |= paths
    body = "".join(t if t.endswith("\n") else t + "\n" for t in texts)
    out = verify_driver.SCRATCH / "issue-fix"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{label}.{hashlib.sha256(body.encode()).hexdigest()[:12]}.patch"
    path.write_text(body)
    return path


# flatten's throwaway repo runs with no user or system configuration and a
# fixed identity, like the lane clone issue_triage/lane_tree.py builds.
_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "prospector", "GIT_AUTHOR_EMAIL": "prospector@localhost",
            "GIT_COMMITTER_NAME": "prospector", "GIT_COMMITTER_EMAIL": "prospector@localhost"}


def _git(repo: Path, *args: str, input: str | None = None) -> str:
    env = {**{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}, **_GIT_ENV}
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, timeout=300, env=env, input=input)
    return done.stdout


def flatten(base_clone: Path, *patches: Path | str | None, label: str) -> Path:
    """One diff from a base with no history, holding `patches` applied in
    order to `base_clone`'s tree in a throwaway one-commit repository. Unlike
    `compose`, patches may touch the same paths, applied to the base in
    sequence. Named and scratched like `compose`. The diff carries full blob ids
    and binary content, so it applies (`--3way` included) in a clone of any
    history size.
    A patch that is None or holds no text contributes nothing and is skipped.
    Raises `ValueError` at the first patch that does not apply, naming its
    index among the patches that carry text."""
    _check_label(label)
    parts = [p for p in patches if p is not None and (isinstance(p, Path) or p.strip())]
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(os.path.realpath(tmp)) / "repo"
        shutil.copytree(base_clone, repo, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "base")
        for i, part in enumerate(parts):
            try:
                if isinstance(part, Path):
                    _git(repo, "apply", "--whitespace=nowarn", str(part.resolve()))
                else:
                    _git(repo, "apply", "--whitespace=nowarn", "-", input=part)
            except subprocess.CalledProcessError as e:
                raise ValueError(f"patch {i} does not apply: {e.stderr.strip()}") from e
        _git(repo, "add", "-N", ".")
        body = _git(repo, "diff", "--full-index", "--binary", "HEAD")
    out = verify_driver.SCRATCH / "issue-fix"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{label}.{hashlib.sha256(body.encode()).hexdigest()[:12]}.patch"
    path.write_text(body)
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


def red_legs(base: PinnedBase, *, patch: Path, test_cmd: str, label: str,
             tail_bytes: int = verify_driver.OUTPUT_TAIL_BYTES) -> Legs:
    """Run the reproduction test over `base` with `patch` applied: the proof is
    a failing first leg confirmed by a second, fresh container."""
    return _legs(base, "red", gates.SENTINEL_TEST_FAIL, patch=patch,
                 test_cmd=test_cmd, label=label, tail_bytes=tail_bytes)


def green_legs(base: PinnedBase, *, patch: Path, test_cmd: str, label: str,
               tail_bytes: int = verify_driver.OUTPUT_TAIL_BYTES) -> Legs:
    """Run the reproduction test over `base` with the test-plus-fix patch
    applied: the proof is a passing first leg confirmed by a second, fresh
    container."""
    return _legs(base, "green", gates.SENTINEL_PASS, patch=patch,
                 test_cmd=test_cmd, label=label, tail_bytes=tail_bytes)


def _legs(base: PinnedBase, phase: Literal["red", "green"], want: int, *,
          patch: Path, test_cmd: str, label: str,
          tail_bytes: int = verify_driver.OUTPUT_TAIL_BYTES) -> Legs:
    """Two legs of `phase`, the second run only when the first exits `want`. Any
    other exit — a timeout's 124 included — is returned as observed in `exit`,
    for the caller's policy to read. `output_tail` is the first leg's; the
    confirm leg's tail is dropped. A probe failure raises: no code runs on a
    sandbox whose isolation is unproven, so such a leg refuses and emits no
    verdict."""
    t0 = time.monotonic()

    def leg() -> tuple[int, str]:
        exit_code, tail = verify_driver.run_phase(
            phase, base.image, patch=patch, tier=base.tier, test_cmd=test_cmd,
            base_sha=base.sha, head_sha=label, tail_bytes=tail_bytes)
        if exit_code == gates.SENTINEL_PROBE_FAIL:
            raise verify_driver.ProbeFailure(
                f"sandbox isolation could not be proven ({label}, phase {phase})")
        return exit_code, tail

    first, tail = leg()
    confirm = leg()[0] if first == want else None
    return Legs(exit=first, exit_confirm=confirm, output_tail=tail,
                duration_s=round(time.monotonic() - t0, 1))


class SuiteFault(RuntimeError):
    """A full-suite run did not complete: a sandbox fault, a timeout, a
    pre-patch that does not apply, or a report the runner did not account for.
    The machine's condition, never a verdict on a fix."""


class SuiteUnplannable(SuiteFault):
    """The tree's own test wrapper cannot derive the suite runner's plan: the
    tree has no full suite this runner can run."""


# How much of a suite run's output a caller keeps: the runner prints its
# end-of-run trailer last, and a long failing set must fit inside the cap.
SUITE_TAIL_BYTES = 64 * 1024


def _tree_key(base: PinnedBase, pre_patch: str | None) -> str:
    """The suite tree's identity: the base, and the pre-patch carried over it."""
    pre = hashlib.sha256((pre_patch or "").encode()).hexdigest()[:16]
    return f"{base.sha[:12]}-{pre}"


def _pre_patch_file(pre_patch: str | None, label: str) -> Path | None:
    return compose(label, pre_patch) if pre_patch else None


def suite_baseline(base: PinnedBase, pre_patch: str | None, *, label: str) -> list[str]:
    """The full suite's own failing files over `base` with `pre_patch` applied,
    cached under the verify scratch by that tree, so each tree's suite runs
    once. Raises SuiteFault when the run does not complete, and RuntimeError
    when the profile carries no suite contract."""
    _check_label(label)
    cache = verify_driver.SCRATCH / "suite-baseline" / f"{_tree_key(base, pre_patch)}.json"
    try:
        cached = json.loads(cache.read_text())
        if isinstance(cached, list) and all(isinstance(f, str) for f in cached):
            return cached
    except (OSError, ValueError):
        pass
    rc, tail = verify_driver.run_phase(
        "baseline", base.image, tier=base.tier, base_sha=base.sha, test_cmd="true",
        suite_config=verify_driver.write_suite_config(),
        pre_patch=_pre_patch_file(pre_patch, label),
        timeout=verify_driver.SUITE_TIMEOUT_SECONDS, tail_bytes=SUITE_TAIL_BYTES)
    if rc == gates.SENTINEL_NO_PLAN:
        raise SuiteUnplannable(f"this tree's test wrapper cannot derive the suite plan: "
                               f"{verify_driver.error_excerpt(tail)}")
    trailer = verify_driver.parse_suite_trailer(tail)
    failed = (trailer or {}).get("failed")
    if (rc != gates.SENTINEL_PASS or trailer is None or trailer.get("mode") != "baseline"
            or not isinstance(failed, list) or not all(isinstance(f, str) for f in failed)):
        raise SuiteFault(f"the suite baseline did not complete (exit {rc}): "
                         f"{verify_driver.error_excerpt(tail)}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(sorted(failed)))
    return sorted(failed)


class SuiteRegress(TypedDict):
    exit: int
    exit_confirm: int | None
    confirmed: bool
    flake: bool
    excluded: int
    new_failures: list[str]


def suite_regress(base: PinnedBase, pre_patch: str | None, patch: str, *,
                  label: str) -> SuiteRegress:
    """The full suite over `base` + `pre_patch` + `patch`, every file its own
    baseline already fails excluded. A failing run is confirmed by a second
    container; a second run that passes reads as a flake. `confirmed` is the
    verdict: the patch makes the suite fail beyond what the tree already
    failed. `new_failures` names the files the runner reported, as evidence.
    Raises SuiteFault when either run does not complete."""
    baseline = suite_baseline(base, pre_patch, label=label)
    exclude = verify_driver.SCRATCH / "suite-exclude" / f"{_tree_key(base, pre_patch)}.json"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(json.dumps(baseline))
    patch_file = compose(label, patch)

    def run() -> tuple[int, str]:
        rc, tail = verify_driver.run_phase(
            "regress", base.image, patch=patch_file, tier=base.tier, base_sha=base.sha,
            test_cmd="true", exclude_file=exclude,
            suite_config=verify_driver.write_suite_config(),
            pre_patch=_pre_patch_file(pre_patch, label),
            timeout=verify_driver.SUITE_TIMEOUT_SECONDS, tail_bytes=SUITE_TAIL_BYTES)
        if rc not in (gates.SENTINEL_PASS, gates.SENTINEL_TEST_FAIL):
            raise SuiteFault(f"the suite run did not complete (exit {rc}): "
                             f"{verify_driver.error_excerpt(tail)}")
        return rc, tail

    first, tail = run()
    out: SuiteRegress = {"exit": first, "exit_confirm": None, "confirmed": False,
                         "flake": False, "excluded": len(baseline), "new_failures": []}
    if first == gates.SENTINEL_TEST_FAIL:
        out["new_failures"] = verify_driver._advisory_failures(tail)
        second, tail2 = run()
        out["exit_confirm"] = second
        out["confirmed"] = second == gates.SENTINEL_TEST_FAIL
        out["flake"] = second == gates.SENTINEL_PASS
        if out["confirmed"]:
            out["new_failures"] = sorted(set(out["new_failures"])
                                         | set(verify_driver._advisory_failures(tail2)))
    return out
