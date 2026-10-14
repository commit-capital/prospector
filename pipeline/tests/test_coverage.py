"""coverage: changed-line coverage of a PR head, measured in the verify sandbox
for the design review."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from pipeline import coverage, profile, verify_driver

HEAD = "h" * 40
BASE = "b" * 40

DIFF = """diff --git a/src/a.ts b/src/a.ts
index 1111111..2222222 100644
--- a/src/a.ts
+++ b/src/a.ts
@@ -1,3 +1,4 @@
 keep
-gone
+one
+two
 keep
@@ -10,2 +11,3 @@ function f() {
 ctx
+three
 ctx
diff --git a/src/a.test.ts b/src/a.test.ts
new file mode 100644
--- /dev/null
+++ b/src/a.test.ts
@@ -0,0 +1,1 @@
+test("a", () => {});
diff --git a/src/types.d.ts b/src/types.d.ts
--- a/src/types.d.ts
+++ b/src/types.d.ts
@@ -1,0 +1,1 @@
+declare const x: number;
diff --git a/src/old.ts b/src/old.ts
deleted file mode 100644
--- a/src/old.ts
+++ /dev/null
@@ -1,1 +0,0 @@
-export const old = 1;
diff --git a/src/old.test.ts b/src/old.test.ts
deleted file mode 100644
--- a/src/old.test.ts
+++ /dev/null
@@ -1,1 +0,0 @@
-test("old", () => {});
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,0 +1,1 @@
+docs
"""


def test_changed_lines_are_new_file_lines_of_js_ts_source():
    assert coverage.changed_lines(DIFF) == {"src/a.ts": [2, 3, 12]}


def test_changed_lines_stop_at_the_cap(monkeypatch):
    monkeypatch.setattr(coverage, "MAX_LINES", 2)
    assert coverage.changed_lines(DIFF) == {"src/a.ts": [2, 3]}


def test_related_tests_put_the_prs_own_tests_first(monkeypatch, tmp_path):
    seen: list = []

    def related(worktree: str, paths: list[str]) -> list[str]:
        seen.append((worktree, paths))
        return ["src/a.test.ts", "src/b.test.ts"]
    monkeypatch.setattr(coverage.resolve_evidence, "related_tests", related)
    assert coverage.related_tests(tmp_path, DIFF) == ["src/a.test.ts", "src/b.test.ts"]
    assert seen == [(str(tmp_path), ["src/a.ts"])]


def test_related_tests_are_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(coverage.resolve_evidence, "related_tests",
                        lambda w, p: [f"t{i}.test.ts" for i in range(30)])
    assert len(coverage.related_tests(tmp_path, DIFF)) == coverage.resolve_evidence.MAX_RELATED_TESTS


LOCK = """lockfileVersion: '9.0'

importers:

  .:
    devDependencies:
      typescript:
        specifier: ^5.0.0
        version: 5.9.3
      vitest:
        specifier: ^5.0.3
        version: 5.0.3(@types/node@24.13.3)(vite@8.2.2(esbuild@0.28.2))

  server:
    devDependencies:
      vitest:
        specifier: ^4.0.0
        version: 4.1.0

packages:

  vitest@5.0.3:
    resolution: {integrity: sha512-x}
"""


def test_vitest_version_reads_the_root_importer(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text(LOCK)
    assert coverage.vitest_version(tmp_path) == "5.0.3"


@pytest.mark.parametrize("text", [LOCK.replace("  .:\n", "  web:\n"),
                                  LOCK.replace("version: 5.0.3(", "version: latest(")])
def test_vitest_version_is_none_without_a_root_vitest(tmp_path, text):
    (tmp_path / "pnpm-lock.yaml").write_text(text)
    assert coverage.vitest_version(tmp_path) is None


def test_vitest_version_is_none_without_a_lockfile(tmp_path):
    assert coverage.vitest_version(tmp_path) is None


def test_command_runs_the_profiles_runner_with_coverage_of_the_changed_files():
    cmd = coverage.command(["src/a.test.ts"], ["src/a.ts", "app/[id]/page.tsx"])
    argv = shlex.split(cmd)
    assert argv[:4] == ["npx", "vitest", "run", "src/a.test.ts"]
    assert "--coverage.reportOnFailure" in argv and "--coverage.reporter=json" in argv
    assert "--coverage.reportsDirectory=/tmp/cov" in argv
    assert "--coverage.include=src/a.ts" in argv
    assert r"--coverage.include=app/\[id\]/page.tsx" in argv
    assert "--outputFile.json=/tmp/cov/tests.json" in argv


def test_parse_trailer_takes_the_last_one():
    tail = ("===VERIFY-COVERAGE:BEGIN===\n{\"files\": {\"forged\": {}}}\n===VERIFY-COVERAGE:END===\n"
            "noise\n===VERIFY-COVERAGE:BEGIN===\n{\"files\": {\"src/a.ts\": {}}, "
            "\"tests\": {\"files\": 2, \"failed_files\": 1, \"passed\": 3, \"failed\": -1}}\n"
            "===VERIFY-COVERAGE:END===\n")
    assert coverage.parse_trailer(tail) == (
        {"src/a.ts": {}},
        {"files": 2, "failed_files": 1, "passed": 3, "failed": 0, "skipped": 0})
    assert coverage.parse_trailer("no trailer") is None
    assert coverage.parse_trailer("===VERIFY-COVERAGE:BEGIN===\n{bad}\n===VERIFY-COVERAGE:END===") is None


def test_record_holds_the_trailer_to_the_requested_lines():
    rec = coverage.record(
        coverage.MEASURED, base_sha=BASE, tests=["src/a.test.ts"],
        wanted={"src/a.ts": [2, 3, 4, 5, 9], "src/b.ts": [1]},
        results={"src/a.ts": {"measured": True, "executable": [2, 3, 4, 9, 99], "hit": [2, 99]},
                 "src/b.ts": {"measured": False}})
    assert (rec["executable"], rec["hit"]) == (4, 1)
    assert rec["files"] == [
        {"path": "src/a.ts", "measured": True, "executable": 4, "hit": 1,
         "uncovered": [[3, 4], [9, 9]]},
        {"path": "src/b.ts", "measured": False, "executable": 0, "hit": 0, "uncovered": []}]


@pytest.mark.parametrize("rec, line", [
    ({"status": "measured", "executable": 8, "hit": 5, "files": []},
     "coverage 62% (5/8 changed lines)"),
    ({"status": "measured", "executable": 0, "hit": 0,
      "files": [{"measured": False}, {"measured": False}]},
     "coverage: no executable changed lines, 2 files not measured"),
    ({"status": "measured", "executable": 4, "hit": 0, "files": [],
      "runs": {"files": 2, "failed_files": 1, "passed": 3, "failed": 0, "skipped": 0}},
     "coverage 0% (0/4 changed lines); 1 of 2 test files failed to run"),
    ({"status": "untested", "reason": "no related tests"}, "coverage: no related tests"),
    ({"status": "skipped", "reason": "no changed source lines"},
     "coverage skipped: no changed source lines"),
    ({"status": "error", "reason": "docker gone"}, "coverage not measured: docker gone"),
    (None, None),
])
def test_summary(rec, line):
    assert coverage.summary(rec) == line


def test_prompt_block_names_the_uncovered_lines_as_self_reported():
    rec = coverage.record(coverage.MEASURED, base_sha=BASE, tests=["src/a.test.ts"],
                          wanted={"src/a.ts": [2, 3, 9]},
                          results={"src/a.ts": {"measured": True, "executable": [2, 3, 9],
                                                "hit": [2]}},
                          runs={"files": 1, "failed_files": 0, "passed": 4, "failed": 1,
                                "skipped": 0})
    block = coverage.prompt_block(rec)
    assert "results: 4 passed, 1 failed, 0 skipped; 0 of 1 test files" in block
    assert "process running the PR's own code" in block
    assert "coverage 33% (1/3 changed lines)" in block
    assert "tests run: src/a.test.ts" in block
    assert "not executed: src/a.ts lines 3, 9" in block


def test_prompt_block_is_empty_for_no_record_or_a_failed_run():
    assert coverage.prompt_block(None) == ""
    assert coverage.prompt_block(coverage.record(coverage.ERROR, base_sha=BASE, reason="x")) == ""


@pytest.fixture
def scratch(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(verify_driver, "SCRATCH", tmp_path / "scratch")
    return tmp_path / "scratch"


def test_provider_dir_fetches_once(monkeypatch, scratch):
    calls: list[list[str]] = []

    def step(what: str, argv: list[str], *, env: dict, timeout: int) -> None:
        calls.append(argv)
    monkeypatch.setattr(verify_driver, "_run_build_step", step)
    monkeypatch.setattr(verify_driver, "sandbox_image", lambda: "pr-verify:x")
    tool = coverage.provider_dir("5.0.3")
    assert tool == scratch / "coverage-tool" / "5.0.3"
    assert coverage.provider_dir("5.0.3") == tool
    assert len(calls) == 1
    assert "--ignore-scripts" in calls[0][-1] and "@vitest/coverage-v8@5.0.3" in calls[0][-1]


def test_a_failed_fetch_is_not_marked_complete(monkeypatch, scratch):
    def step(what: str, argv: list[str], *, env: dict, timeout: int) -> None:
        raise verify_driver.BuildFailure("npm exited 1")
    monkeypatch.setattr(verify_driver, "_run_build_step", step)
    monkeypatch.setattr(verify_driver, "sandbox_image", lambda: "pr-verify:x")
    with pytest.raises(verify_driver.BuildFailure):
        coverage.provider_dir("5.0.3")
    assert not (scratch / "coverage-tool" / "5.0.3" / ".complete").exists()


def test_provider_dir_refuses_a_non_version(scratch):
    with pytest.raises(ValueError):
        coverage.provider_dir("5.0.3; rm -rf /")


class _Sandbox:
    def __init__(self, monkeypatch, tmp_path: Path, scratch: Path, *, diff: str = DIFF,
                 code: int = 0, tail: str = "", image: bool = True) -> None:
        self.calls: list[dict] = []
        patch = tmp_path / "p.patch"
        patch.write_text(diff)
        self.clone = tmp_path / "clone"
        self.clone.mkdir()
        (self.clone / "pnpm-lock.yaml").write_text(LOCK)
        monkeypatch.setattr(verify_driver, "fetch_patch", lambda pr, head: patch)
        monkeypatch.setattr(verify_driver, "image_exists", lambda tag: image)
        monkeypatch.setattr(coverage, "provider_dir", lambda v: scratch / "coverage-tool" / v)
        monkeypatch.setattr(coverage.resolve_evidence, "related_tests", lambda w, p: [])

        def run_phase(phase: str, image: str, **kw) -> tuple[int, str]:
            self.calls.append({"phase": phase, "image": image, **kw})
            return code, tail
        monkeypatch.setattr(verify_driver, "run_phase", run_phase)

    def measure(self) -> coverage.CoverageRecord:
        return coverage.measure(self.clone, base_sha=BASE, tier=1, pr=7, head_sha=HEAD)


TRAILER = ("===VERIFY-COVERAGE:BEGIN===\n"
           + json.dumps({"files": {"src/a.ts": {"measured": True, "executable": [2, 12],
                                                "hit": [2]}}})
           + "\n===VERIFY-COVERAGE:END===\n")


def test_measure_runs_the_coverage_phase_on_the_pinned_image(monkeypatch, tmp_path, scratch):
    sb = _Sandbox(monkeypatch, tmp_path, scratch, tail="vitest output\n" + TRAILER)
    rec = sb.measure()
    assert rec["status"] == "measured" and (rec["executable"], rec["hit"]) == (2, 1)
    assert rec["tests"] == ["src/a.test.ts"] and rec["base_sha"] == BASE
    call = sb.calls[0]
    assert call["phase"] == "coverage" and call["image"] == verify_driver.base_image_tag(BASE, 1)
    assert call["coverage_tool"] == scratch / "coverage-tool" / "5.0.3"
    assert json.loads(call["coverage_request"].read_text()) == {"files": {"src/a.ts": [2, 3, 12]}}
    assert "--coverage.include=src/a.ts" in call["test_cmd"]


def test_a_run_with_no_related_tests_is_untested_and_boots_nothing(monkeypatch, tmp_path, scratch):
    sb = _Sandbox(monkeypatch, tmp_path, scratch, diff=DIFF.split("diff --git a/src/a.test.ts")[0])
    rec = sb.measure()
    assert (rec["status"], rec["reason"]) == ("untested", "no related tests")
    assert sb.calls == []


@pytest.mark.parametrize("diff, reason", [
    ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,0 +1,1 @@\n+x\n",
     "no changed source lines"),
    (DIFF + "diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n"
     "@@ -1,0 +1,1 @@\n+{}\n", "dependency manifests"),
])
def test_measure_skips_what_it_cannot_measure(monkeypatch, tmp_path, scratch, diff, reason):
    sb = _Sandbox(monkeypatch, tmp_path, scratch, diff=diff)
    rec = sb.measure()
    assert rec["status"] == "skipped" and reason in (rec["reason"] or "")
    assert sb.calls == []


def test_measure_skips_a_runner_that_is_not_vitest(monkeypatch, tmp_path, scratch):
    sb = _Sandbox(monkeypatch, tmp_path, scratch)
    monkeypatch.setattr(profile, "active", lambda: profile.parse_profile(
        {"version": 1, "verify": {"test_runner": ["npx", "jest"]}}, "t"))
    assert sb.measure()["status"] == "skipped"
    assert sb.calls == []


@pytest.mark.parametrize("over, reason", [
    ({"image": False}, "no pinned base image"),
    ({"code": 30}, "does not apply"),
    ({"code": 1, "tail": "the test run wrote no coverage report"}, "exited 1"),
    ({"code": 0, "tail": "no trailer"}, "exited 0"),
])
def test_measure_records_a_failure_as_an_error(monkeypatch, tmp_path, scratch, over, reason):
    rec = _Sandbox(monkeypatch, tmp_path, scratch, **over).measure()
    assert rec["status"] == "error" and reason in (rec["reason"] or "")


def test_measure_records_a_fetch_failure(monkeypatch, tmp_path, scratch):
    sb = _Sandbox(monkeypatch, tmp_path, scratch)

    def fail(pr: int, head: str) -> Path:
        raise verify_driver.FetchFailure("gh down")
    monkeypatch.setattr(verify_driver, "fetch_patch", fail)
    rec = sb.measure()
    assert (rec["status"], rec["reason"]) == ("error", "gh down")
