"""Changed-line coverage: of the lines a PR adds to non-test source, how many its
related tests execute — the design review's deterministic companion, measured
in the verify sandbox on this machine's pinned base and stored beside the
review as `reviews["design"].extra.coverage`.

The target repository installs no coverage provider and the sandbox installs
offline, so the provider (`@vitest/coverage-v8` at the repository's own vitest
version) is fetched once per version into the verify scratch and mounted
read-only into the `coverage` phase, which copies it into the tree.

Everything the phase reports is written by a process running the PR's code,
so the figure is advisory evidence: shown, and handed to the design reviewer,
never a gate.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import TypedDict

from pipeline import diffpaths, gates, profile, resolve_evidence, verify_driver, verify_gc

SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
# Changed lines measured per PR, files kept on the record, and uncovered
# ranges kept per file.
MAX_LINES = 2000
MAX_FILES = 50
MAX_RANGES = 20
TIMEOUT_SECONDS = 900
TAIL_BYTES = 64 * 1024
FETCH_TIMEOUT_SECONDS = 600
REASON_CHARS = 300

MEASURED, UNTESTED, SKIPPED, ERROR = "measured", "untested", "skipped", "error"
# A run this machine could not carry out — a full disk, a missing image, a
# harness exit — books a fault on the design lane's health, never on the PR.
FAULT = "fault"
_FAULT_EXITS = frozenset({gates.SENTINEL_PATCH_UNREADABLE, gates.SENTINEL_SETUP_FAIL})
_FAULT_REASON = re.compile(rf"^the coverage run exited ({'|'.join(map(str, sorted(_FAULT_EXITS)))}):")
# A design review whose coverage faulted is taken again once this long has passed.
FAULT_RETRY_HOURS = 6

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_TRAILER_RE = re.compile(
    r"===VERIFY-COVERAGE:BEGIN===\s*(\{.*?\})\s*===VERIFY-COVERAGE:END===", re.DOTALL)
_GLOB_CHARS_RE = re.compile(r"([\[\]{}()*?!+@])")


class CoverageFile(TypedDict):
    path: str
    measured: bool
    executable: int
    hit: int
    uncovered: list[list[int]]


class TestRuns(TypedDict):
    files: int
    failed_files: int
    passed: int
    failed: int
    skipped: int


class CoverageRecord(TypedDict):
    status: str
    reason: str | None
    base_sha: str
    tests: list[str]
    runs: TestRuns | None
    executable: int
    hit: int
    files: list[CoverageFile]
    duration_s: float


def is_source(path: str) -> bool:
    return (path.endswith(SOURCE_SUFFIXES) and not path.endswith(".d.ts")
            and not diffpaths.is_test_path(path))


def _kept(text: str) -> list[tuple[str, str]]:
    """The diff's blocks for files the PR adds or modifies, not deletes."""
    return [(p, b) for p, b in diffpaths.diff_blocks(text) if "\n+++ /dev/null" not in b]


def changed_lines(text: str) -> dict[str, list[int]]:
    """The new-file line numbers each JS/TS source file the diff adds or
    modifies gains, at most MAX_LINES in all."""
    out: dict[str, list[int]] = {}
    budget = MAX_LINES
    for path, block in _kept(text):
        if not is_source(path) or budget <= 0:
            continue
        lines: list[int] = []
        line = None
        for raw in block.split("\n"):
            m = _HUNK_RE.match(raw)
            if m:
                line = int(m.group(1))
            elif line is None or raw.startswith(("+++", "\\")):
                continue
            elif raw.startswith("+"):
                if budget > 0:
                    lines.append(line)
                    budget -= 1
                line += 1
            elif raw.startswith(" "):
                line += 1
        if lines:
            out[path] = lines
    return out


def related_tests(clone: Path, text: str) -> list[str]:
    """The test files to run: the PR's own added or modified tests, then the
    base's tests related to its changed source paths, at most
    resolve_evidence.MAX_RELATED_TESTS."""
    kept = [p for p, _ in _kept(text)]
    own = [p for p in kept if diffpaths.is_test_path(p) and p.endswith(SOURCE_SUFFIXES)]
    sources = [p for p in kept if is_source(p)]
    picked = list(own)
    for t in resolve_evidence.related_tests(str(clone), sources) if sources else []:
        if t not in picked:
            picked.append(t)
    return picked[:resolve_evidence.MAX_RELATED_TESTS]


def vitest_version(clone: Path) -> str | None:
    """The vitest version the root importer of `clone`'s pnpm-lock.yaml
    resolves, or None when it names none."""
    try:
        lines = (clone / "pnpm-lock.yaml").read_text().split("\n")
    except OSError:
        return None
    in_importers = in_root = in_vitest = False
    for line in lines:
        if not line.strip():
            continue
        if not line.startswith(" "):
            in_importers = line.rstrip() == "importers:"
            in_root = in_vitest = False
            continue
        if not in_importers:
            continue
        if line.startswith("  ") and not line.startswith("   "):
            in_root, in_vitest = line.strip() == ".:", False
        elif in_root and line.strip() == "vitest:":
            in_vitest = True
        elif in_vitest and line.strip().startswith("version:"):
            version = line.split(":", 1)[1].strip().strip("'\"").split("(", 1)[0]
            return version if _VERSION_RE.fullmatch(version) else None
    return None


def provider_dir(version: str) -> Path:
    """The fetched `@vitest/coverage-v8@<version>` directory, fetched on first
    use from a networked container as the sandbox image's unprivileged user,
    with no lifecycle scripts. Raises verify_driver.BuildFailure when the
    fetch fails."""
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"not a version: {version!r}")
    tool = verify_driver.SCRATCH / "coverage-tool" / version
    done = tool / ".complete"
    with verify_gc.generation_lock(tool):
        if done.is_file():
            return tool
        tool.mkdir(parents=True, exist_ok=True)
        script = ("cd /out && rm -rf node_modules package-lock.json"
                  " && echo '{\"private\":true}' > package.json"
                  " && npm install --ignore-scripts --omit=peer --no-audit --no-fund"
                  f" @vitest/coverage-v8@{version}"
                  " && test -f node_modules/@vitest/coverage-v8/package.json")
        verify_driver._run_build_step(
            f"fetching @vitest/coverage-v8@{version}",
            ["docker", "run", "--rm", "-v", f"{tool}:/out", verify_driver.sandbox_image(),
             "bash", "-lc", script],
            env=verify_driver.launcher_env(), timeout=FETCH_TIMEOUT_SECONDS)
        done.touch()
    return tool


def _include(path: str) -> str:
    return "--coverage.include=" + _GLOB_CHARS_RE.sub(r"\\\1", path)


def command(tests: list[str], files: list[str]) -> str:
    """The profile's runner over `tests` with v8 coverage of `files` written
    as istanbul JSON to /tmp/cov — reported even when a test fails — and the
    test report beside it."""
    v = profile.active().verify
    parts = [*v.test_runner, *tests, *v.test_flags, "--coverage.enabled",
             "--coverage.provider=v8", "--coverage.reporter=json",
             "--coverage.reportOnFailure", "--coverage.reportsDirectory=/tmp/cov",
             *(_include(f) for f in files), "--reporter=default", "--reporter=json",
             "--outputFile.json=/tmp/cov/tests.json"]
    return " ".join(shlex.quote(p) for p in parts)


def parse_trailer(tail: str) -> tuple[dict[str, dict], TestRuns | None] | None:
    """The per-file results and the test counts of the last coverage trailer in
    `tail`, or None when it carries no per-file results."""
    found = _TRAILER_RE.findall(tail)
    if not found:
        return None
    try:
        parsed = json.loads(found[-1])
    except ValueError:
        return None
    files = parsed.get("files") if isinstance(parsed, dict) else None
    if not isinstance(files, dict):
        return None
    raw = parsed.get("tests")
    runs: TestRuns | None = None
    if isinstance(raw, dict):
        def n(key: str) -> int:
            v = raw.get(key)
            return v if isinstance(v, int) and v >= 0 else 0
        runs = {"files": n("files"), "failed_files": n("failed_files"), "passed": n("passed"),
                "failed": n("failed"), "skipped": n("skipped")}
    return files, runs


def _ranges(lines: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for n in sorted(lines):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out[:MAX_RANGES]


def _ints(value: object, allowed: set[int]) -> list[int]:
    return [n for n in value if isinstance(n, int) and n in allowed] if isinstance(value, list) else []


def record(status: str, *, base_sha: str, reason: str | None = None,
           tests: list[str] | None = None, wanted: dict[str, list[int]] | None = None,
           results: dict[str, dict] | None = None, runs: TestRuns | None = None,
           duration_s: float = 0.0) -> CoverageRecord:
    """The stored record. For a measured run, each file's counts are the
    trailer's lines held to the ones the host asked about."""
    files: list[CoverageFile] = []
    for path, lines in (wanted or {}).items() if results is not None else ():
        got = results.get(path) if results is not None else None
        allowed = set(lines)
        if not isinstance(got, dict) or got.get("measured") is not True:
            files.append({"path": path, "measured": False, "executable": 0, "hit": 0,
                          "uncovered": []})
            continue
        executable = _ints(got.get("executable"), allowed)
        hit = _ints(got.get("hit"), set(executable))
        files.append({"path": path, "measured": True, "executable": len(executable),
                      "hit": len(hit), "uncovered": _ranges(sorted(set(executable) - set(hit)))})
    return {"status": status, "reason": reason[:REASON_CHARS] if reason else None,
            "base_sha": base_sha, "tests": list(tests or []), "runs": runs,
            "executable": sum(f["executable"] for f in files),
            "hit": sum(f["hit"] for f in files), "files": files[:MAX_FILES],
            "duration_s": round(duration_s, 1)}


def measure(clone: Path, *, base_sha: str, tier: int, pr: int, head_sha: str) -> CoverageRecord:
    """Measure PR `pr`'s changed-line coverage at `head_sha` on this machine's
    pinned base. Never raises: a failure is a `fault` record when it is this
    machine's, else an `error` record."""
    t0 = time.monotonic()

    def done(status: str, reason: str | None = None, *, tests: list[str] | None = None,
             wanted: dict[str, list[int]] | None = None,
             results: dict[str, dict] | None = None,
             runs: TestRuns | None = None) -> CoverageRecord:
        return record(status, base_sha=base_sha, reason=reason, tests=tests, wanted=wanted,
                      results=results, runs=runs, duration_s=time.monotonic() - t0)

    try:
        if "vitest" not in " ".join(profile.active().verify.test_runner):
            return done(SKIPPED, "the profile's test runner is not vitest")
        patch = verify_driver.fetch_patch(pr, head_sha)
        text = patch.read_text(errors="replace")
        if gates.deps_touched(diffpaths.changed_paths(text)):
            return done(SKIPPED, "the PR changes dependency manifests, which the sandbox "
                                 "never installs")
        wanted = changed_lines(text)
        if not wanted:
            return done(SKIPPED, "no changed source lines")
        tests = related_tests(clone, text)
        if not tests:
            return done(UNTESTED, "no related tests")
        version = vitest_version(clone)
        if version is None:
            return done(ERROR, "the base's pnpm-lock.yaml names no vitest version")
        image = verify_driver.base_image_tag(base_sha, tier)
        if not verify_driver.image_exists(image):
            return done(FAULT, f"this machine has no pinned base image {image}")
        tool = provider_dir(version)
        req_dir = verify_driver.SCRATCH / "coverage"
        req_dir.mkdir(parents=True, exist_ok=True)
        req = req_dir / f"pr-{pr}-{head_sha[:12]}.json"
        req.write_text(json.dumps({"files": wanted}))
        code, tail = verify_driver.run_phase(
            "coverage", image, patch=patch, tier=tier, test_cmd=command(tests, list(wanted)),
            base_sha=base_sha, head_sha=head_sha, timeout=TIMEOUT_SECONDS,
            tail_bytes=TAIL_BYTES, coverage_tool=tool, coverage_request=req)
    except (ValueError, verify_driver.DiffTooLarge) as e:
        return done(ERROR, str(e), tests=[])
    except (verify_driver.FetchFailure, verify_driver.BuildFailure, OSError,
            subprocess.SubprocessError) as e:
        return done(FAULT, str(e), tests=[])
    if code == gates.SENTINEL_PATCH_CONFLICT:
        return done(ERROR, "the PR does not apply onto this machine's pinned base", tests=tests)
    parsed = parse_trailer(tail) if code == 0 else None
    if parsed is None:
        excerpt = verify_driver.error_excerpt(tail)
        return done(FAULT if code in _FAULT_EXITS else ERROR,
                    f"the coverage run exited {code}: {excerpt}", tests=tests)
    return done(MEASURED, tests=tests, wanted=wanted, results=parsed[0], runs=parsed[1])


def is_fault(rec: CoverageRecord | dict | None) -> bool:
    """Whether `rec` is a run this machine could not carry out, including an
    `error` record stamped with a harness exit."""
    if not rec:
        return False
    return rec.get("status") == FAULT or (
        rec.get("status") == ERROR and bool(_FAULT_REASON.match(rec.get("reason") or "")))


def summary(rec: CoverageRecord | dict | None) -> str | None:
    """One line for the Checks row, or None when there is no record."""
    if not rec:
        return None
    status, reason = rec.get("status"), rec.get("reason") or ""
    if status == MEASURED:
        executable, hit = int(rec.get("executable") or 0), int(rec.get("hit") or 0)
        unmeasured = sum(1 for f in rec.get("files") or [] if not f.get("measured"))
        line = (f"coverage {round(100 * hit / executable)}% ({hit}/{executable} changed lines)"
                if executable else "coverage: no executable changed lines")
        if unmeasured:
            line += f", {unmeasured} file{'' if unmeasured == 1 else 's'} not measured"
        runs = rec.get("runs") or {}
        if runs.get("failed_files"):
            line += f"; {runs['failed_files']} of {runs.get('files')} test files failed to run"
        return line
    if status == UNTESTED:
        return "coverage: no related tests"
    if status == SKIPPED:
        return f"coverage skipped: {reason}"
    if is_fault(rec):
        return f"coverage could not run (worker fault, not this PR): {reason[:120]}"
    return f"coverage not measured: {reason[:120]}"


def prompt_block(rec: CoverageRecord | None) -> str:
    """The design reviewer's evidence block, or "" when there is nothing to
    weigh (no record, or a run that failed)."""
    if rec is None or rec["status"] in (ERROR, FAULT):
        return ""
    lines = ["Changed-line coverage, measured by running the PR's related tests in a "
             "sandbox. The numbers are reported by a process running the PR's own code: "
             "evidence for the `test` judgment, not proof.",
             f"- {summary(rec)}"]
    if rec["tests"]:
        lines.append("- tests run: " + ", ".join(rec["tests"]))
    runs = rec.get("runs")
    if runs:
        lines.append(f"- results: {runs['passed']} passed, {runs['failed']} failed, "
                     f"{runs['skipped']} skipped; {runs['failed_files']} of {runs['files']} "
                     "test files could not run in the sandbox")
    for f in rec["files"]:
        if f["uncovered"]:
            spans = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in f["uncovered"])
            lines.append(f"- not executed: {f['path']} lines {spans}")
        elif not f["measured"]:
            lines.append(f"- not measured: {f['path']}")
    return "\n".join(lines) + "\n\n"
