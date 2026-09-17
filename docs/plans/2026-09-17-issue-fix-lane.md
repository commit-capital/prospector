# Issue Funnel and Issue-Fix Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Issues work queues beside the PR queues — a funnel that finds uncovered, reproducible, worth-fixing issues, and a lane in which first-party agents reproduce, fix, prove, and propose a fix as a PR that then flows through the existing PR pipeline.

**Architecture:** Two tracks built in parallel. The **lane** track (L1–L8) adds pinned-base sandbox primitives, an issue-side request queue with compare-and-swap claims, isolated agent stages (reproduce → judge → fix → review) whose outcomes the host decides from sandbox exits, blind trials against open PRs as the go-live gate, and one new gated upstream write (fork branch + App-opened PR). The **funnel** track (F1–F10) derives fresh issue→PR links and coverage on read, adds deterministic tier-0 facts, a tool-less ASSESS rubric, deterministic candidacy and ranking, a needs-info loop, and just-in-time FIX-MATCH / FIND-FIXED.

**Tech Stack:** Python 3.14 / SQLAlchemy / FastAPI / pytest; React + TS frontend (pnpm); Docker sandbox; headless Claude CLI.

**Spec:** `docs/specs/2026-09-17-issue-fix-lane-design.md` — normative for vocabulary, record shapes, states and endings, gates, agent contracts, propose fences, threat controls.

**This document details the first step of each track (L1 and F1).** Every later step gets its own plan file when it starts (`docs/plans/<date>-issue-fix-<step>.md`), written against the code as it then stands; the roadmap at the end names them.

## Global Constraints

- No existing PR queue, gate, or fence changes behavior. A shared function gains a seam only with a golden test pinning the PR path byte for byte.
- pyright at 0 errors: `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`
- `uv run ruff check .` at 0 findings; `uv run pytest` green; `python -c "import prospector_app.backend.app"` boots.
- Frontend (when touched): from `prospector_app/frontend`, `pnpm run build` 0 tsc errors; no new eslint errors (`pnpm exec eslint <files>`).
- Comments and docstrings describe present code only — no history, no counterfactuals ("instead of", "would otherwise"). Precise type annotations; no quoted annotations; qualified imports (`from pipeline import …`, `from issue_triage import …`). Match the comment density of the surrounding code.
- `pipeline/` and `issue_triage/` never import `prospector_app`.
- All new issue policy lives in `issue_triage/issue_gates.py`; derived state is computed on read, never stored.
- Tests follow the package's style: module-level `def test_*(tmp_path, monkeypatch)` over a real SQLite `IssueStore(tmp_path)` in `issue_triage/tests`; mocks for the sandbox and the agent CLI.
- A `STORE_SCHEMA_VERSION` bump locks older checkouts out of writing the shared store: land it, then update every machine on that store together.

---

## Part A — L1: sandbox primitives and seams

### Task 1: `pipeline/check_records.py` — path-keyed check records

**Files:**
- Create: `pipeline/check_records.py`
- Modify: `prospector_app/backend/sandbox_check.py` (`CheckRecord`, `record_check`, `collect_checks`)
- Test: `pipeline/tests/test_check_records.py`; golden: `prospector_app/backend/tests/test_sandbox_check.py`, `test_fix_worker.py` (unchanged, must stay green)

**Interfaces:**
- Produces: `check_records.CheckRecord` (TypedDict, moved verbatim), `check_records.append(path: Path, record: CheckRecord) -> None`, `check_records.collect(path: Path, limit: int) -> list[dict]`. `sandbox_check.CheckRecord` stays importable.

- [ ] **Step 1: Write the failing tests** (`pipeline/tests/test_check_records.py`)

```python
"""Path-keyed JSONL records of an authoring agent's sandbox runs."""
from __future__ import annotations

import json

from pipeline import check_records


def _rec(kind: str = "test") -> check_records.CheckRecord:
    return {"kind": kind, "files": ["a.test.ts"], "cmd": "npx vitest run a.test.ts",
            "exit": 20, "error_kind": None, "error": None, "error_excerpt": "boom",
            "duration_s": 1.5, "at": "2026-09-17T00:00:00+00:00"}


def test_append_then_collect_round_trips_oldest_first_and_consumes(tmp_path):
    p = tmp_path / "deep" / "issue-7.checks.jsonl"
    check_records.append(p, _rec("typecheck"))
    check_records.append(p, _rec("test"))
    got = check_records.collect(p, 20)
    assert [r["kind"] for r in got] == ["typecheck", "test"]
    assert not p.exists()
    assert check_records.collect(p, 20) == []


def test_collect_skips_lines_that_are_not_json_objects_and_caps(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("not json\n[1]\n" + "".join(json.dumps(_rec()) + "\n" for _ in range(5)))
    assert len(check_records.collect(p, 3)) == 3


def test_append_to_an_unwritable_path_reports_and_does_not_raise(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    check_records.append(blocker / "child.jsonl", _rec())
    assert "could not be recorded" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest pipeline/tests/test_check_records.py -q` → `ModuleNotFoundError: pipeline.check_records`.

- [ ] **Step 3: Implement `pipeline/check_records.py`**

```python
"""Bounded JSONL records of the sandbox runs an authoring agent made, keyed by
the file they are kept in. The agent's check tool appends one record per run;
the worker that launched the agent collects them once it returns."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict


class CheckRecord(TypedDict):
    """One sandbox run the authoring agent made, as the request stores it.
    `exit` is the sandbox's sentinel exit when the command ran and None when
    it did not; `error_kind` names why it did not — the preflight's own kind
    when it gives one, `refused` for a run the preflight declined to make, else
    `infrastructure`."""
    kind: str
    files: list[str]
    cmd: str | None
    exit: int | None
    error_kind: str | None
    error: str | None
    error_excerpt: str | None
    duration_s: float | None
    at: str


def append(path: Path, record: CheckRecord) -> None:
    """Append `record` to `path`. Best-effort: a record that cannot be written
    is reported on stderr and the run's verdict still reaches the agent."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        print(f"sandbox-check: the run could not be recorded: {e}", file=sys.stderr)


def collect(path: Path, limit: int) -> list[dict]:
    """The records in `path`, oldest first and at most `limit`, consuming the
    file so the next request starts empty. A line that is not a JSON object is
    skipped."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    path.unlink(missing_ok=True)
    out: list[dict] = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[:limit]
```

- [ ] **Step 4: Delegate from `sandbox_check.py`** — delete its `CheckRecord` class body and import it (`from pipeline.check_records import CheckRecord`, plus `check_records` in the `from pipeline import …` line); `record_check(pr, record)` becomes `check_records.append(checks_path(pr), record)`; `collect_checks(pr)` becomes `return check_records.collect(checks_path(pr), MAX_CHECKS)`. Keep both docstrings; drop the now-unused `json` import only if nothing else uses it. `checks_path`, `discard_checks`, `check_record`, `main` are untouched.

- [ ] **Step 5: Run** — `uv run pytest pipeline/tests/test_check_records.py prospector_app/backend/tests/test_sandbox_check.py prospector_app/backend/tests/test_fix_worker.py -q` → all pass (the `pr-<n>.checks.jsonl` name and consume-on-collect behavior are pinned by the existing tests).

- [ ] **Step 6: Commit** — `git commit -m "Keep an agent's sandbox-run records in a path-keyed module the issue lane can share"`

### Task 2: `verify_driver.validate_test_files` — the authored-test file rules, standalone

**Files:**
- Modify: `pipeline/verify_driver.py` (`validate_authored`, ~:1181)
- Test: `pipeline/tests/test_verify_driver.py` (new `TestValidateTestFiles`; existing `TestValidateAuthored` is the golden guard)

**Interfaces:**
- Produces: `verify_driver.validate_test_files(files: list[wire.VerifyAuthoredFile], expected_red_signature: str | None, *, base_clone: Path, taken_paths: list[str]) -> tuple[str | None, str | None]` — `(derived command, None)` or `(None, reason)`; reasons `file-count-not-1-to-3`, `malformed-file-entry`, `path-not-repo-relative`, `path-not-a-test-path`, `path-exists-on-base`, `path-taken`, `duplicate-paths`, `contents-too-large`, `no-expected-red-signature`.
- `validate_authored` keeps its signature and every reason string it returns today (`path-taken` reads as `path-in-pr-diff` there).

- [ ] **Step 1: Write the failing tests** (append to `pipeline/tests/test_verify_driver.py`)

```python
class TestValidateTestFiles:
    """The file rules alone: the issue lane validates reproduction tests with
    no PR and no AuthorItem."""

    FILES = [{"path": "ui/src/pages/routine-toast.test.tsx", "contents": "test('t', () => {})\n"}]

    def test_valid_files_derive_the_command(self, tmp_path):
        cmd, why = vd.validate_test_files(self.FILES, "AssertionError: x",
                                          base_clone=tmp_path, taken_paths=[])
        assert why is None and cmd is not None and "routine-toast.test.tsx" in cmd

    def test_a_taken_path_is_refused(self, tmp_path):
        _, why = vd.validate_test_files(self.FILES, "sig", base_clone=tmp_path,
                                        taken_paths=["ui/src/pages/routine-toast.test.tsx"])
        assert why == "path-taken"

    def test_validate_authored_still_names_the_pr_diff(self, tmp_path):
        _, why = vd.validate_authored(_author_item(), base_clone=tmp_path,
                                      pr_paths=["ui/src/pages/routine-toast.test.tsx"])
        assert why == "path-in-pr-diff"

    def test_a_missing_signature_is_refused(self, tmp_path):
        _, why = vd.validate_test_files(self.FILES, None, base_clone=tmp_path, taken_paths=[])
        assert why == "no-expected-red-signature"
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest pipeline/tests/test_verify_driver.py -k ValidateTestFiles -q` → `AttributeError: validate_test_files`.

- [ ] **Step 3: Implement** — move the body of `validate_authored` below its `can_author` check into `validate_test_files`, renaming `pr_set` to `taken` and its reason to `"path-taken"`; `validate_authored` becomes:

```python
    if not item.can_author:
        return None, "agent-declined"
    cmd, why = validate_test_files(item.files, item.expected_red_signature,
                                   base_clone=base_clone, taken_paths=pr_paths)
    return cmd, "path-in-pr-diff" if why == "path-taken" else why
```

`validate_test_files` takes the rules paragraph of the docstring ("Authored paths must be NEW files under the active profile's test conventions — absent from the pinned base clone and disjoint from `taken_paths` … The command is derived from the paths by derive_test_command"); `validate_authored`'s docstring keeps its first paragraph and says the file rules are `validate_test_files`'.

- [ ] **Step 4: Run** — `uv run pytest pipeline/tests/test_verify_driver.py -k "ValidateAuthored or ValidateTestFiles" -q` → all pass.

- [ ] **Step 5: Commit** — `git commit -m "Validate authored test files without a PR or an author item"`

### Task 3: `headless_agent` — scoped reads and an environment allowlist

**Files:**
- Modify: `pipeline/headless_agent.py` (`_flags` ~:137, `run_agent` ~:292, module docstring)
- Test: `pipeline/tests/test_headless_agent.py`

**Interfaces:**
- Produces: `_flags(allow_gh, edit_root=None, allow=(), read_root: str | None = None)`; `run_agent(..., read_root: str | None = None, env_allow: Sequence[str] | None = None)`. With both `None`, the argv and environment are today's.
- Spike S7 (spec, "Spikes") fixed the rule shape: `Read(//root/**)`, `Grep(//root/**)`, `Glob(//root/**)`; no git rules beside a `read_root`.

- [ ] **Step 1: Write the failing tests**

```python
def test_flags_without_read_root_are_the_bare_read_tools():
    flags = ha._flags(False)
    assert flags[flags.index("--allowedTools") + 1] == "Read,Grep,Glob"


def test_flags_read_root_scopes_all_three_read_tools_to_the_real_root(tmp_path):
    real = tmp_path / "clone"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    flags = ha._flags(False, read_root=f"{link}/")
    allowed = flags[flags.index("--allowedTools") + 1].split(",")
    target = real.resolve()
    assert allowed[:3] == [f"Read(/{target}/**)", f"Grep(/{target}/**)", f"Glob(/{target}/**)"]
    assert "Read" not in allowed and "Grep" not in allowed and "Glob" not in allowed


def test_flags_read_root_withholds_git_beside_an_edit_root(tmp_path):
    flags = ha._flags(False, edit_root=str(tmp_path), read_root=str(tmp_path))
    allowed = flags[flags.index("--allowedTools") + 1]
    assert "Edit(" in allowed and "Bash(git" not in allowed


def test_flags_edit_root_alone_keeps_read_only_git(tmp_path):
    allowed = ha._flags(False, edit_root=str(tmp_path))
    assert "Bash(git diff:*)" in allowed[allowed.index("--allowedTools") + 1]


def test_run_agent_env_allow_keeps_the_clis_needs_the_named_and_the_extra(monkeypatch):
    seen: dict = {}

    def fake_popen(cmd, **kw):
        seen["env"] = kw["env"]
        return _FakeProc(cmd)

    monkeypatch.setattr(ha.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgres://secret")
    monkeypatch.setenv("DOCKER_HOST", "unix:///x.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("HOME", "/home/w")
    ha.run_agent("p", allow_gh=False, cwd="/", env_allow=["DOCKER_HOST"],
                 env_extra={"PROSPECTOR_ISSUE_CHECK_ISSUE": "7"})
    env = seen["env"]
    assert "TRIAGE_STORE_URL" not in env
    assert env["DOCKER_HOST"] == "unix:///x.sock" and env["ANTHROPIC_API_KEY"] == "k"
    assert env["HOME"] == "/home/w" and env["PROSPECTOR_ISSUE_CHECK_ISSUE"] == "7"


def test_run_agent_without_env_allow_inherits_the_environment(monkeypatch):
    seen: dict = {}

    def fake_popen(cmd, **kw):
        seen["env"] = kw["env"]
        return _FakeProc(cmd)

    monkeypatch.setattr(ha.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgres://x")
    ha.run_agent("p", allow_gh=False, cwd="/")
    assert seen["env"]["TRIAGE_STORE_URL"] == "postgres://x"
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest pipeline/tests/test_headless_agent.py -k "read_root or env_allow or bare_read or edit_root_alone" -q`.

- [ ] **Step 3: Implement**

```python
# What the CLI itself reads from its environment to start and authenticate.
_CLI_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TERM")
_CLI_ENV_PREFIXES = ("LC_", "ANTHROPIC_", "CLAUDE_")


def _flags(allow_gh: bool, edit_root: str | None = None,
           allow: Sequence[str] = (), read_root: str | None = None) -> list[str]:
    if read_root:
        # One rule per read tool: a scoped Read beside a bare Grep or Glob
        # leaves those two reading the whole host.
        scope = "/" + os.path.realpath(read_root).rstrip("/")
        reads = [f"Read({scope}/**)", f"Grep({scope}/**)", f"Glob({scope}/**)"]
    else:
        reads = ["Read", "Grep", "Glob"]
    tools = [*reads, *(_GH_ALLOW if allow_gh else []), *allow]
    disallowed = list(_DISALLOWED)
    if edit_root:
        # (existing comment block, unchanged)
        root = "/" + os.path.realpath(edit_root).rstrip("/")
        tools += [f"Edit({root}/**)", f"Write({root}/**)"]
        # `git diff --no-index <file> /dev/null` prints any host file, so an
        # agent whose reads are scoped gets no git.
        if not read_root:
            tools += _GIT_READ_ALLOW
        disallowed = [t for t in disallowed if t not in ("Edit", "Write")]
    ...


def _agent_env(env_allow: Sequence[str] | None,
               env_extra: Mapping[str, str] | None) -> dict[str, str]:
    env = operator_env()
    if env_allow is not None:
        keep = {*_CLI_ENV, *env_allow}
        env = {k: v for k, v in env.items()
               if k in keep or k.startswith(_CLI_ENV_PREFIXES)}
    if env_extra:
        env.update(env_extra)
    return env
```

`run_agent` passes `read_root` to `_flags` and replaces its three env lines with `env = _agent_env(env_allow, env_extra)`; extend its docstring with one sentence each for `read_root` and `env_allow`, and the module docstring's closing sentence with the scoped-read grant.

- [ ] **Step 4: Run** — `uv run pytest pipeline/tests/test_headless_agent.py -q` → all pass (the existing `_flags` tests are the golden guard for today's callers).

- [ ] **Step 5: Commit** — `git commit -m "Let a headless agent's reads and environment be scoped to what its caller names"`

### Task 4: `pipeline/prove.py` — red and green on the pinned base

**Files:**
- Create: `pipeline/prove.py`
- Test: `pipeline/tests/test_prove.py`

**Interfaces:**
- Consumes: `verify_driver.run_phase`, `_pin`, `base_image_tag`, `base_clone_dir`, `daemon_available`, `image_exists`, `base_command_failure`, `error_excerpt`, `ProbeFailure`, `SCRATCH`; `gates.SENTINEL_*`, `gates.deps_touched`, `gates.base_fault_text`; `diffpaths.changed_paths`.
- Produces:

```python
class NoBase(RuntimeError): ...
@dataclass(frozen=True)
class PinnedBase:
    sha: str
    tier: int
    image: str
    clone: Path
def pinned(store: Store) -> PinnedBase
def compose(label: str, *parts: Path | str | None) -> Path
def run_command(base: PinnedBase, patch: Path, cmd: str, *,
                phase: Literal["green", "compile"], label: str) -> dict
class Legs(TypedDict):
    exit: int | None
    exit_confirm: int | None
    output_tail: str
    duration_s: float
def red_legs(base: PinnedBase, *, patch: Path, test_cmd: str, label: str) -> Legs
def green_legs(base: PinnedBase, *, patch: Path, test_cmd: str, label: str) -> Legs
```

- [ ] **Step 1: Write the failing tests** (`pipeline/tests/test_prove.py`; `run_phase` is mocked — no Docker)

```python
"""Pinned-base proof primitives. The sandbox is mocked: what is pinned here is
which phases run, over which patch, and how their exits read."""
from __future__ import annotations

import pytest

from pipeline import gates, prove, verify_driver as vd

BASE = prove.PinnedBase(sha="a" * 40, tier=1, image="pr-verify-base:aaaaaaaaaaaa-t1",
                        clone=__import__("pathlib").Path("/nonexistent"))
TEST = ("diff --git a/x.test.ts b/x.test.ts\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/x.test.ts\n@@ -0,0 +1,1 @@\n+test\n")
FIX = "diff --git a/src/x.ts b/src/x.ts\n--- a/src/x.ts\n+++ b/src/x.ts\n@@ -1 +1 @@\n-a\n+b\n"


@pytest.fixture
def phases(monkeypatch, tmp_path):
    monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(prove, "SCRATCH", tmp_path / "scratch")
    calls: list[dict] = []
    exits: list[int] = []

    def fake(phase, image, **kw):
        calls.append({"phase": phase, "image": image, **kw})
        return exits.pop(0), f"tail of {phase}"

    monkeypatch.setattr(vd, "run_phase", fake)
    return calls, exits


def test_compose_concatenates_in_order_with_newlines(phases, tmp_path):
    out = prove.compose("issue-7", TEST.rstrip("\n"), None, FIX)
    assert out.read_text() == TEST + FIX
    assert out.parent == tmp_path / "scratch" / "issue-fix"


def test_compose_refuses_parts_that_touch_a_common_path(phases):
    with pytest.raises(ValueError, match="src/x.ts"):
        prove.compose("issue-7", FIX, FIX)


def test_red_confirms_only_after_a_failing_first_leg(phases):
    calls, exits = phases
    exits[:] = [gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL]
    legs = prove.red_legs(BASE, patch=prove.compose("i", TEST), test_cmd="t", label="issue-7")
    assert (legs["exit"], legs["exit_confirm"]) == (20, 20)
    assert [c["phase"] for c in calls] == ["red", "red"]
    assert calls[0]["base_sha"] == BASE.sha and calls[0]["head_sha"] == "issue-7"
    assert calls[0]["tier"] == 1 and calls[0]["image"] == BASE.image


def test_red_that_passes_runs_no_confirm(phases):
    calls, exits = phases
    exits[:] = [gates.SENTINEL_PASS]
    legs = prove.red_legs(BASE, patch=prove.compose("i", TEST), test_cmd="t", label="l")
    assert (legs["exit"], legs["exit_confirm"]) == (0, None) and len(calls) == 1


def test_green_confirms_only_after_a_passing_first_leg(phases):
    calls, exits = phases
    exits[:] = [gates.SENTINEL_PASS, gates.SENTINEL_PASS]
    legs = prove.green_legs(BASE, patch=prove.compose("i", TEST, FIX), test_cmd="t", label="l")
    assert (legs["exit"], legs["exit_confirm"]) == (0, 0)
    assert [c["phase"] for c in calls] == ["green", "green"]


def test_a_probe_failure_raises(phases):
    _, exits = phases
    exits[:] = [gates.SENTINEL_PROBE_FAIL]
    with pytest.raises(vd.ProbeFailure):
        prove.red_legs(BASE, patch=prove.compose("i", TEST), test_cmd="t", label="l")


def test_run_command_refuses_a_dependency_manifest(phases, tmp_path):
    p = tmp_path / "d.patch"
    p.write_text("diff --git a/package.json b/package.json\n--- a/package.json\n"
                 "+++ b/package.json\n@@ -1 +1 @@\n-a\n+b\n")
    rec = prove.run_command(BASE, p, "pnpm -r typecheck", phase="compile", label="l")
    assert "refused" in rec and "exit" not in rec


def test_run_command_reads_a_base_that_fails_the_compile_as_the_lanes_fault(phases, monkeypatch):
    _, exits = phases
    exits[:] = [gates.SENTINEL_TEST_FAIL]
    monkeypatch.setattr(vd, "base_command_failure", lambda image, cmd, run: "exit 20: TS2304")
    rec = prove.run_command(BASE, prove.compose("i", FIX), "pnpm -r typecheck",
                            phase="compile", label="l")
    assert rec["exit"] == 20 and rec["error_kind"] == "base-compile"


def test_pinned_raises_no_base_without_an_image(monkeypatch):
    monkeypatch.setattr(vd, "_pin", lambda store: ("b" * 40, 1))
    monkeypatch.setattr(vd, "daemon_available", lambda: True)
    monkeypatch.setattr(vd, "image_exists", lambda image: False)
    with pytest.raises(prove.NoBase, match="image"):
        prove.pinned(object())
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest pipeline/tests/test_prove.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement `pipeline/prove.py`**

```python
"""Host-observed proof on this machine's pinned base: a test patch that fails
(red), a test-plus-fix patch that passes (green), and a command over a patched
tree. Every run uses the image the verify pin names, so a caller never builds
one. The exits are the verdict; the captured output is evidence."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from pipeline import diffpaths, gates, verify_driver
from pipeline.verify_driver import SCRATCH

if TYPE_CHECKING:
    from pipeline.store import Store


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
```

`run_command` mirrors `compile_preflight._compile_over` with the base fixed to `base` (record keys `cmd`, `label`, `base_sha`, `exit`, `error`, `error_kind`, `error_excerpt`, `refused`, `output_tail`, `duration_s`; an empty or manifest-touching patch is `refused`; on `SENTINEL_TEST_FAIL` in the `compile` phase it asks `verify_driver.base_command_failure(base.image, cmd, <pristine compile run>)` and records `error_kind="base-compile"` with `gates.base_fault_text`; exits 40 and other non-sentinels record `error`). `_legs(phase, want)` runs `verify_driver.run_phase(phase, base.image, patch=patch, tier=base.tier, test_cmd=test_cmd, base_sha=base.sha, head_sha=label)`, raises `verify_driver.ProbeFailure` on `SENTINEL_PROBE_FAIL`, and runs the confirm leg only when the first exit equals `want`; `red_legs` wants `SENTINEL_TEST_FAIL`, `green_legs` wants `SENTINEL_PASS`.

- [ ] **Step 4: Run** — `uv run pytest pipeline/tests/test_prove.py -q` → all pass; then `uv run pyright pipeline` → 0 errors.

- [ ] **Step 5: Commit** — `git commit -m "Prove a test red and a fix green on the machine's pinned base"`

- [ ] **Step 6: L1 gate battery** — `uv run pytest -q`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`, `uv run ruff check .`, `uv run python -c "import prospector_app.backend.app"`.

---

## Part B — F1: fresh issue→PR links

### Task 5: `issue_triage/pr_index.py` — the PR store's links, inverted

**Files:**
- Create: `issue_triage/pr_index.py`
- Test: `issue_triage/tests/test_pr_index.py`

**Interfaces:**
- Consumes: `pipeline.model.Pr` (`n`, `state`, `draft`, `title`, `updated_at`, `head_sha`, `linked_issues` — entries `{issue, pain, how}`).
- Produces: `pr_index.DIRECT = ("explicit", "body-ref")`; `class PrLink(TypedDict)` with `pr: int`, `how: str`, `state: str | None`, `draft: bool`, `title: str | None`, `updated_at: str | None`, `head_sha: str | None`; `pr_index.build(prs: Iterable[Pr]) -> dict[int, list[PrLink]]`.

- [ ] **Step 1: Write the failing tests**

```python
from pipeline.model import Pr
from issue_triage import pr_index


def _pr(n, linked, state="open", draft=False):
    return Pr(None, {"pr": n, "meta": {"title": f"PR {n}", "state": state, "draft": draft,
                                       "head_sha": f"h{n}", "updated_at": "2026-09-01T00:00:00Z"},
                     "issues": {"linked": linked}})


def test_build_inverts_direct_links_only():
    idx = pr_index.build([
        _pr(10, [{"issue": 1, "how": "explicit"}, {"issue": 2, "how": "subsystem"}]),
        _pr(11, [{"issue": 1, "how": "body-ref"}], state="merged"),
    ])
    assert sorted(idx) == [1]
    assert [(l["pr"], l["how"], l["state"]) for l in idx[1]] == [
        (10, "explicit", "open"), (11, "body-ref", "merged")]
    assert idx[1][0]["head_sha"] == "h10" and idx[1][0]["draft"] is False


def test_explicit_beats_body_ref_for_the_same_pr_and_issue():
    idx = pr_index.build([_pr(10, [{"issue": 1, "how": "body-ref"},
                                   {"issue": 1, "how": "explicit"}])])
    assert [l["how"] for l in idx[1]] == ["explicit"]
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest issue_triage/tests/test_pr_index.py -q`.

- [ ] **Step 3: Implement**

```python
"""Issue → the pull requests whose own bodies link it, inverted from the PR
store's `issues.linked` sections. Every PR ingest path refreshes those from live
PR bodies, so this index is as fresh as the PR store itself."""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from pipeline.model import Pr

# The link kinds a PR's own body establishes.
DIRECT = ("explicit", "body-ref")


class PrLink(TypedDict):
    pr: int
    how: str
    state: str | None
    draft: bool
    title: str | None
    updated_at: str | None
    head_sha: str | None


def build(prs: Iterable[Pr]) -> dict[int, list[PrLink]]:
    """Issue number → its direct PR links, one per PR (explicit over body-ref),
    ordered by PR number."""
    best: dict[tuple[int, int], PrLink] = {}
    for pr in prs:
        for entry in pr.linked_issues:
            how = entry.get("how")
            if how not in DIRECT or entry.get("issue") is None:
                continue
            key = (int(entry["issue"]), pr.n)
            if key in best and best[key]["how"] == "explicit":
                continue
            best[key] = {"pr": pr.n, "how": how, "state": pr.state, "draft": pr.draft,
                         "title": pr.title, "updated_at": pr.updated_at,
                         "head_sha": pr.head_sha}
    out: dict[int, list[PrLink]] = {}
    for (issue, _), link in sorted(best.items()):
        out.setdefault(issue, []).append(link)
    return out
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** — `git commit -m "Index the PR store's direct issue links by issue"`

### Task 6: `issue_triage/issue_links.py` — the one merged accessor

**Files:**
- Create: `issue_triage/issue_links.py`
- Modify: `issue_triage/issue_model.py` (read property `github_links`)
- Test: `issue_triage/tests/test_issue_links.py`

**Interfaces:**
- Consumes: `Issue.candidate_prs`, `pr_index.PrLink`, `pr_index.DIRECT`.
- Produces: `Issue.github_links -> list[dict]` — `(self.rec.get("links") or {}).get("github") or []`, beside `candidate_prs`.
- Produces: `issue_links.HOW_RANK: dict[str, int]` (`explicit` 0, `github` 0, `fix-found` 1, `fix-match` 2, `issue-ref` 3, `body-ref` 4, `subsystem` 5); `how_rank(cand: dict) -> int` (unknown → 6); `referenced(cand: dict) -> bool` (`explicit | github | fix-found | fix-match | issue-ref`); `linked_prs(issue: Issue, pr_links: list[PrLink] | None) -> list[dict]` — `{pr, how, title}` dicts plus `state` / `draft` when the source knows them, one per PR, strongest kind wins, ordered by `(how_rank, pr)`.

Rules: the PR index owns `DIRECT` kinds — with an index (a list, possibly empty) stored `explicit` / `body-ref` candidates are dropped and the index's entries stand; with `None` (index unavailable) the stored snapshot stands. `github` entries come from `issue.github_links`. Issue-owned kinds (`issue-ref`, `subsystem`, `fix-found`, `fix-match`) always come from the store.

- [ ] **Step 1: Write the failing tests**

```python
from issue_triage import issue_links
from issue_triage.issue_model import Issue


def _issue(candidates, github=None):
    links = {"candidates": candidates}
    if github is not None:
        links["github"] = github
    return Issue(None, {"issue": 1, "meta": {"title": "t", "state": "open",
                                             "updated_at": "2026-09-01T00:00:00Z"},
                        "links": links})


def _link(pr, how, state="open"):
    return {"pr": pr, "how": how, "state": state, "draft": False, "title": f"PR {pr}",
            "updated_at": None, "head_sha": None}


def test_the_index_replaces_stored_direct_kinds_and_keeps_issue_owned_ones():
    iss = _issue([{"pr": 10, "how": "explicit", "title": "stale"},
                  {"pr": 11, "how": "subsystem", "title": "s"},
                  {"pr": 12, "how": "fix-found", "title": "f"}])
    got = issue_links.linked_prs(iss, [_link(20, "explicit")])
    assert [(c["pr"], c["how"]) for c in got] == [(20, "explicit"), (12, "fix-found"),
                                                  (11, "subsystem")]


def test_without_an_index_the_stored_snapshot_stands():
    iss = _issue([{"pr": 10, "how": "explicit", "title": "x"}])
    assert [c["pr"] for c in issue_links.linked_prs(iss, None)] == [10]


def test_github_closing_references_rank_with_explicit_and_strongest_kind_wins():
    iss = _issue([{"pr": 30, "how": "subsystem", "title": "s"}],
                 github=[{"pr": 30, "state": "open", "draft": False}])
    got = issue_links.linked_prs(iss, [])
    assert [(c["pr"], c["how"]) for c in got] == [(30, "github")]


def test_referenced_excludes_tag_and_bare_body_matches():
    assert issue_links.referenced({"how": "github"}) and issue_links.referenced({"how": "fix-match"})
    assert not issue_links.referenced({"how": "subsystem"})
    assert not issue_links.referenced({"how": "body-ref"})
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** `issue_links.py` per the rules above (a `by_pr: dict[int, dict]` merge keeping the lower `how_rank`; `github` entries built as `{"pr", "how": "github", "title": "", "state", "draft"}`).

- [ ] **Step 4: Run** → pass. **Step 5: Commit** — `git commit -m "Read an issue's linked PRs through one accessor over the store and the PR index"`

### Task 7: ingest the issue-owned link facts

**Files:**
- Modify: `issue_triage/fetch_issues.py` (`_ISSUES_QUERY`, `normalize_gql`, `normalize_issue`), `issue_triage/issue_ingest.py` (`_meta`, `_facts_unchanged`, `ingest_records`), `issue_triage/issue_model.py` (`assignees`, `last_edited_at`, `_links_with`, `apply_facts`, `set_links`, `record_fixed`)
- Test: `issue_triage/tests/test_fetch_normalize.py`, `test_issue_ingest.py`, `test_issue_model.py`

**Interfaces:**
- Consumes: `Issue.github_links` (Task 6).
- Produces: normalized raw keys `assignees: list[str]`, `last_edited_at: str | None`, `github_links: list[dict] | None` (`[{pr, state, draft}]`; `None` from the REST refetch, which cannot see closing references); `Issue.assignees`, `Issue.last_edited_at`; `Issue.apply_facts(..., github: list[dict] | None = None)`; `links` writers preserve sibling keys.

- [ ] **Step 1: Failing tests**

```python
def test_normalize_gql_carries_assignees_edit_time_and_closing_references():
    node = {"number": 5, "title": "t", "state": "OPEN", "updatedAt": "u",
            "lastEditedAt": "e", "assignees": {"nodes": [{"login": "dev"}]},
            "closedByPullRequestsReferences": {"nodes": [
                {"number": 9, "state": "OPEN", "isDraft": True}]}}
    raw = fetch_issues.normalize_gql(node)
    assert raw["assignees"] == ["dev"] and raw["last_edited_at"] == "e"
    assert raw["github_links"] == [{"pr": 9, "state": "open", "draft": True}]


def test_the_rest_refetch_reports_closing_references_unknown():
    raw = fetch_issues.normalize_issue({"number": 5, "assignees": [{"login": "dev"}]})
    assert raw["github_links"] is None and raw["assignees"] == ["dev"]


def test_ingest_rewrites_when_only_the_closing_references_change(tmp_path):
    store = IssueStore(tmp_path)
    raw = dict(RAW, github_links=[])
    assert issue_ingest.ingest_records(store, [raw], []) == 1
    assert issue_ingest.ingest_records(store, [raw], []) == 0
    raw2 = dict(raw, github_links=[{"pr": 9, "state": "open", "draft": False}])
    assert issue_ingest.ingest_records(store, [raw2], []) == 1
    assert store.load_issue(raw["number"]).github_links == raw2["github_links"]


def test_unknown_closing_references_keep_the_stored_ones(tmp_path):
    store = IssueStore(tmp_path)
    issue_ingest.ingest_records(store, [dict(RAW, github_links=[{"pr": 9, "state": "open", "draft": False}])], [])
    issue_ingest.ingest_records(store, [dict(RAW, title="edited", github_links=None)], [])
    assert [g["pr"] for g in store.load_issue(RAW["number"]).github_links] == [9]


def test_record_fixed_preserves_github_links(tmp_path):
    store = IssueStore(tmp_path)
    issue_ingest.ingest_records(store, [dict(RAW, github_links=[{"pr": 9, "state": "open", "draft": False}])], [])
    store.edit_issue(RAW["number"]).record_fixed(44, rationale="r")
    iss = store.load_issue(RAW["number"])
    assert [g["pr"] for g in iss.github_links] == [9]
    assert any(c["pr"] == 44 and c["how"] == "fix-found" for c in iss.candidate_prs)
```

(`RAW` is the module's existing normalized-issue fixture; add one if the file has none: `number`, `title`, `body`, `state="open"`, `labels=[]`, `comments=0`, `reactions_total=0`, `thumbs_up=0`, `author`, `created_at`, `updated_at`, `state_reason=None`, `assignees=[]`, `last_edited_at=None`.)

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**
  - `_ISSUES_QUERY` nodes gain `lastEditedAt`, `assignees(first:10) { nodes { login } }`, `closedByPullRequestsReferences(first:10, includeClosedPrs:true) { nodes { number state isDraft } }`.
  - `normalize_gql` / `normalize_issue` add the three keys (GraphQL states lowercased).
  - `issue_ingest._meta` adds `assignees` and `last_edited_at`.
  - `Issue._links_with(**changes) -> dict` returns the stored `links` section's own keys (minus `checked_at`) updated with `changes`; `apply_facts`, `set_links`, `record_fixed` stamp `links` through it. `apply_facts(..., github=None)` sets `github` only when given.
  - `_facts_unchanged(iss, meta, summary, repro, github)` also requires `github is None or (iss.rec.get("links") or {}).get("github", []) == github`.
  - `ingest_records` passes `raw.get("github_links")` through.

- [ ] **Step 4: Run** `uv run pytest issue_triage/tests -q` → pass. **Step 5: Commit** — `git commit -m "Ingest each issue's assignees, edit time, and GitHub closing references"`

### Task 8: `ingest_records` writes the current record, compare-and-swap

**Files:**
- Modify: `issue_triage/issue_store.py` (`stamped_issue`, `save_issue_if`), `issue_triage/issue_model.py` (`stage_facts`; `apply_facts` = stage + persist), `issue_triage/issue_ingest.py` (`ingest_records` write loop)
- Test: `issue_triage/tests/test_issue_ingest.py`, `test_issue_store.py`

**Interfaces:**
- Produces: `IssueStore.stamped_issue(n: int) -> tuple[Issue, str | None] | None`; `IssueStore.save_issue_if(issue: Issue, expected_saved_at: str | None) -> bool`; `Issue.stage_facts(meta, *, summary=None, repro=None, links=None, github=None) -> None` (no persist).

- [ ] **Step 1: Failing tests**

```python
def test_ingest_keeps_a_section_written_after_its_snapshot_was_taken(tmp_path, monkeypatch):
    store = IssueStore(tmp_path)
    issue_ingest.ingest_records(store, [RAW], [])
    real = store.all_issues

    def snapshot_then_concurrent_write(**kw):
        snap = real(**kw)
        store.edit_issue(RAW["number"]).record_fix_scan("not-fixed", rationale="mid-ingest")
        return snap

    monkeypatch.setattr(store, "all_issues", snapshot_then_concurrent_write)
    issue_ingest.ingest_records(store, [dict(RAW, title="edited")], [])
    iss = store.load_issue(RAW["number"])
    assert iss.title == "edited" and (iss.fix_scan or {}).get("status") == "not-fixed"


def test_save_issue_if_refuses_a_stale_stamp(tmp_path):
    store = IssueStore(tmp_path)
    issue_ingest.ingest_records(store, [RAW], [])
    iss, stamp = store.stamped_issue(RAW["number"])
    store.edit_issue(RAW["number"]).record_fix_scan("not-fixed")
    assert store.save_issue_if(iss, stamp) is False
```

- [ ] **Step 2: Run to verify they fail** (the first fails today: the write-back drops `fix_scan`).

- [ ] **Step 3: Implement** — `stamped_issue` / `save_issue_if` wrap `self._issues.stamped` / `save_if`. In `ingest_records`, for a changed issue that exists:

```python
            for _ in range(2):
                got = store.stamped_issue(n)
                if got is None:
                    break
                iss, stamp = got
                iss.stage_facts(meta, summary=summary, repro=repro, links=links, github=github)
                if store.save_issue_if(iss, stamp):
                    break
            else:
                raise RuntimeError(f"issue #{n} kept changing under ingest")
```

and a new issue is created with `Issue(store, {"issue": int(n)}).apply_facts(...)`. The comparison still runs on the light snapshot; the docstring says each changed issue is re-read in full and written only over the record it read.

- [ ] **Step 4: Run** `uv run pytest issue_triage/tests -q` → pass. **Step 5: Commit** — `git commit -m "Write an ingested issue over the record as it stands, not over the run's snapshot"`

### Task 9: the app reads links through the accessor; schema 23

**Files:**
- Modify: `prospector_app/backend/issues.py` (`_how_rank`, `_referenced`, `_cluster_linked_prs`, `_issue_linked_prs`, `_row`, `close_fixed_gate`), `prospector_app/backend/chat.py` (`_issue_context`'s linked-PR lines, `_HOW_LABEL` gains `github`), `issue_triage/issue_analyze_driver.py` and `issue_fixed_driver.py` (`_issue_bundle` candidate lists), `pipeline/schema.py` (version + changelog), `prospector_app/frontend/src/views/Issues.tsx` (`EVIDENCE` map gains `github`, `body-ref`), `prospector_app/frontend/src/api.ts` (`IssuePR.how` union)
- Test: `prospector_app/backend/tests/test_issues*.py`, `issue_triage/tests/test_issue_analyze_driver.py`, `test_issue_fixed_driver.py`

**Interfaces:**
- Consumes: `pr_index.build`, `issue_links.linked_prs / how_rank / referenced`.
- Produces: `issues._pr_links() -> dict[int, list[PrLink]] | None` — the index over `data.prs()`, cached on `data.generation()`, `None` while `data.snapshot_loading()`.

- [ ] **Step 1: Failing tests** — (a) a row for an issue whose stored candidates are empty shows a `linked_prs` entry and `referenced_pr_count == 1` when the PR snapshot holds a PR with `issues.linked = [{issue: n, how: "explicit"}]`; (b) `close_fixed_gate(n, pr)` accepts that PR (stubbing `_live_pr_states` → merged, `_live_state` → open) where today it answers "is not a fix candidate"; (c) while the snapshot is loading the row falls back to stored candidates; (d) the ANALYZE bundle's `candidate_prs` for that issue names the PR.

- [ ] **Step 2: Implement** — every `iss.candidate_prs` read in the files above becomes `issue_links.linked_prs(iss, links)` with `links = (_pr_links() or {}).get(iss.number, [])` when the index is available and `None` otherwise; `_HOW_RANK` / `_referenced` delegate to `issue_links`; drivers build the index once per bundle from `Store().all_prs().values()`.

- [ ] **Step 3: Schema** — `STORE_SCHEMA_VERSION = 23`; changelog line: `23 — an issue's links section carries sibling keys beside candidates (github closing references); an older ingest or find-fixed write replaces the section whole and drops them.` Backfill lines 20–22 from `git log -S"STORE_SCHEMA_VERSION = 2" --oneline -- pipeline/schema.py` (one line each, naming what older code mishandles).

- [ ] **Step 4: Frontend** — add `github` ("closing reference") and `body-ref` to the evidence map and the `how` union; `pnpm run build`; `pnpm exec eslint src/views/Issues.tsx src/api.ts`.

- [ ] **Step 5: Docs** — `CLAUDE.md` (the pipeline section's issue paragraph: links are derived on read through `issue_links.linked_prs`), `issue_triage/README.md` phase table, `ARCHITECTURE.md` if it names `links.candidates`.

- [ ] **Step 6: F1 gate battery**, then verify in the running app (preview `prospector serve --dev`): the Issues tab's Linked PRs column shows a PR opened after the last issue ingest; **Commit** — `git commit -m "Read issue-to-PR links fresh from the PR store everywhere they are shown or gated on"`

---

## Roadmap — lightweight first; each step planned in its own file when it starts

1. **Lane core as a command** (spec: Agent contracts, Sandbox primitives, Policy — `reproduction_outcome`, `fix_patch_regate`, `fix_proof_bar`): `issue_triage/fix_lane.py`, `reproduce_issue.py`, `judge_repro.py`, `fix_issue.py`, `review_issue_fix.py`, `prospector_app/agent/issue-sandbox-check`, a `python -m issue_triage.fix_lane --issue N` entry that writes a result file and one ledger row. Run spike S12 first. No store sections, queue, worker, or UI.
2. **Replay v0** (spec: Evaluation, "History replay", and Build order step 3): one script over a list of (issue, merged PR) pairs on the pin's image, R6 instance validation, the merged PR's tests as the oracle, a markdown table and ledger rows. `patchkit.flatten` / `fresh_repo` land here.
3. **Decision with the owner on the numbers.**
4. As justified, in this order: queue + records + worker + panel (spec: Store, Lane state machine, Worker and control, App surface) · blind trials against open PRs · propose, built dry-run (spikes S1–S6, S8–S11 before it goes live) · hunter and budgets · unattended propose bar.

Funnel after F1: the uncovered-issues view (`issue_gates.coverage` over deterministic links, a filter and a chip). The rest of the funnel (threads, profile `issues` section and forms, `grade_repro` v2, ASSESS, `fix_candidacy`, `issue_automation.classify`, needs-info, FIX-MATCH, FIND-FIXED upgrades, `issue_sweep`) waits for the replay to show which report features predict a reproduction.
