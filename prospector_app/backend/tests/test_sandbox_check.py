"""The fix author's sandbox check: the agent chooses a lane and test files, and
nothing else — the tree it measures comes from the environment the worker set,
and the command it runs is the profile's. The sandbox itself is mocked."""
from __future__ import annotations

import subprocess

import pytest

from pipeline import compile_preflight, profile
from prospector_app.backend import sandbox_check

TYPECHECK = profile.parse_profile(
    {"version": 1, "verify": {"compile_cmd": "pnpm -r typecheck"}}, "t")
BARE = profile.parse_profile({"version": 1}, "t")


@pytest.fixture
def typecheck_profile(monkeypatch):
    monkeypatch.setattr(profile, "active", lambda: TYPECHECK)


def test_typecheck_is_the_profiles_compile_command(typecheck_profile):
    assert sandbox_check.lane_command(["typecheck"]) == ("pnpm -r typecheck", None)


def test_typecheck_without_a_configured_command_is_refused(monkeypatch):
    monkeypatch.setattr(profile, "active", lambda: BARE)
    cmd, why = sandbox_check.lane_command(["typecheck"])
    assert cmd is None and "no typecheck command" in why


def test_test_lane_builds_the_profiles_runner_over_the_named_files(typecheck_profile):
    cmd, why = sandbox_check.lane_command(
        ["test", "src/a.test.ts", "packages/x/src/b.test.ts"])
    assert why is None
    assert cmd.startswith("npx vitest run src/a.test.ts packages/x/src/b.test.ts")
    assert "--testTimeout=120000" in cmd


@pytest.mark.parametrize("path", ["/etc/passwd", "../escape.test.ts",
                                  "src/not-a-test.ts", "a/../../b.test.ts"])
def test_test_lane_refuses_paths_that_are_not_repo_relative_tests(typecheck_profile, path):
    cmd, why = sandbox_check.lane_command(["test", path])
    assert cmd is None and path in why


@pytest.mark.parametrize("argv", [[], ["test"], ["build"], ["typecheck", "x"]])
def test_anything_else_is_a_usage_error(typecheck_profile, argv):
    cmd, why = sandbox_check.lane_command(argv)
    assert cmd is None and why == sandbox_check.USAGE


def test_combined_patch_is_the_pr_diff_with_the_edits_appended(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "scratch"))
    pr_patch = tmp_path / "pr.patch"
    pr_patch.write_text("diff --git a/pr.ts b/pr.ts\n+pr")
    out = sandbox_check.combined_patch(7, pr_patch, "diff --git a/edit.ts b/edit.ts\n+e")
    assert out.read_text() == ("diff --git a/pr.ts b/pr.ts\n+pr\n"
                               "diff --git a/edit.ts b/edit.ts\n+e\n")


def test_render_reads_pass_fail_and_conflict():
    assert sandbox_check.render({"cmd": "c", "exit": 0, "duration_s": 1}).endswith("PASS")
    assert "FAIL\nTS2345" in sandbox_check.render(
        {"cmd": "c", "exit": 20, "duration_s": 1, "error_excerpt": "TS2345"})
    assert "do not apply" in sandbox_check.render({"cmd": "c", "exit": 30, "duration_s": 1})
    assert sandbox_check.render({"refused": "deps"}).startswith("check refused")
    assert sandbox_check.render({"error": "boom"}).startswith("check could not run")


def test_main_refuses_outside_the_fix_author(monkeypatch, capsys):
    for k in ("PROSPECTOR_CHECK_PR", "PROSPECTOR_CHECK_HEAD",
              "PROSPECTOR_CHECK_WORKTREE", "PROSPECTOR_CHECK_PR_PATCH"):
        monkeypatch.delenv(k, raising=False)
    assert sandbox_check.main(["typecheck"]) == 2
    assert "PROSPECTOR_CHECK_" in capsys.readouterr().err


def test_main_runs_the_lane_over_pr_plus_edits(tmp_path, monkeypatch, typecheck_profile,
                                               capsys):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "scratch"))
    pr_patch = tmp_path / "pr.patch"
    pr_patch.write_text("diff --git a/pr.ts b/pr.ts\n+pr\n")
    monkeypatch.setenv("PROSPECTOR_CHECK_PR", "7")
    monkeypatch.setenv("PROSPECTOR_CHECK_HEAD", "a" * 40)
    monkeypatch.setenv("PROSPECTOR_CHECK_WORKTREE", "/wt")
    monkeypatch.setenv("PROSPECTOR_CHECK_PR_PATCH", str(pr_patch))
    monkeypatch.setattr(sandbox_check, "authored_patch",
                        lambda wt: "diff --git a/edit.ts b/edit.ts\n+e\n")
    seen: dict = {}

    def fake_run(pr, head, patch, cmd):
        seen.update(pr=pr, head=head, text=patch.read_text(), cmd=cmd)
        return {"cmd": cmd, "exit": 0, "duration_s": 2.0}
    monkeypatch.setattr(compile_preflight, "run_command_for_patch", fake_run)

    assert sandbox_check.main(["typecheck"]) == 0
    assert seen["pr"] == 7 and seen["head"] == "a" * 40
    assert seen["cmd"] == "pnpm -r typecheck"
    assert seen["text"] == "diff --git a/pr.ts b/pr.ts\n+pr\ndiff --git a/edit.ts b/edit.ts\n+e\n"
    assert "PASS" in capsys.readouterr().out


def test_a_failing_lane_exits_nonzero_with_the_excerpt(tmp_path, monkeypatch,
                                                       typecheck_profile, capsys):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "scratch"))
    pr_patch = tmp_path / "pr.patch"
    pr_patch.write_text("diff --git a/pr.ts b/pr.ts\n+pr\n")
    monkeypatch.setenv("PROSPECTOR_CHECK_PR", "7")
    monkeypatch.setenv("PROSPECTOR_CHECK_HEAD", "a" * 40)
    monkeypatch.setenv("PROSPECTOR_CHECK_WORKTREE", "/wt")
    monkeypatch.setenv("PROSPECTOR_CHECK_PR_PATCH", str(pr_patch))
    monkeypatch.setattr(sandbox_check, "authored_patch", lambda wt: "")
    monkeypatch.setattr(compile_preflight, "run_command_for_patch",
                        lambda pr, head, patch, cmd: {"cmd": cmd, "exit": 20,
                                                      "duration_s": 2.0,
                                                      "error_excerpt": "TS2345 nope"})
    assert sandbox_check.main(["typecheck"]) == 1
    assert "TS2345 nope" in capsys.readouterr().out


def test_an_unreadable_worktree_is_a_usage_failure_not_a_run(tmp_path, monkeypatch,
                                                             typecheck_profile, capsys):
    pr_patch = tmp_path / "pr.patch"
    pr_patch.write_text("x\n")
    monkeypatch.setenv("PROSPECTOR_CHECK_PR", "7")
    monkeypatch.setenv("PROSPECTOR_CHECK_HEAD", "a" * 40)
    monkeypatch.setenv("PROSPECTOR_CHECK_WORKTREE", str(tmp_path / "missing"))
    monkeypatch.setenv("PROSPECTOR_CHECK_PR_PATCH", str(pr_patch))
    monkeypatch.setattr(compile_preflight, "run_command_for_patch",
                        lambda *a: pytest.fail("sandbox reached"))
    monkeypatch.setattr(sandbox_check, "authored_patch",
                        lambda wt: (_ for _ in ()).throw(
                            subprocess.CalledProcessError(128, "git")))
    assert sandbox_check.main(["typecheck"]) == 2
    assert "worktree" in capsys.readouterr().err


# --- the record the worker collects ------------------------------------------------

def _under_author(monkeypatch, tmp_path, pr: int = 7) -> None:
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "scratch"))
    pr_patch = tmp_path / "pr.patch"
    pr_patch.write_text("diff --git a/pr.ts b/pr.ts\n+pr\n")
    monkeypatch.setenv("PROSPECTOR_CHECK_PR", str(pr))
    monkeypatch.setenv("PROSPECTOR_CHECK_HEAD", "a" * 40)
    monkeypatch.setenv("PROSPECTOR_CHECK_WORKTREE", "/wt")
    monkeypatch.setenv("PROSPECTOR_CHECK_PR_PATCH", str(pr_patch))
    monkeypatch.setattr(sandbox_check, "authored_patch", lambda wt: "")


def test_every_run_is_recorded_for_the_worker_to_collect(tmp_path, monkeypatch,
                                                         typecheck_profile, capsys):
    _under_author(monkeypatch, tmp_path)
    runs = iter([
        {"cmd": "pnpm -r typecheck", "exit": 0, "duration_s": 12.5},
        {"cmd": "npx vitest run src/a.test.ts", "exit": 20, "duration_s": 3.0,
         "error_excerpt": "expected 1 to be 2"},
        {"cmd": "npx vitest run src/a.test.ts", "duration_s": 0.4,
         "error": "TimeoutError: the sandbox lock was held for 600s"},
    ])
    monkeypatch.setattr(compile_preflight, "run_command_for_patch",
                        lambda pr, head, patch, cmd: next(runs))

    assert sandbox_check.main(["typecheck"]) == 0
    assert sandbox_check.main(["test", "src/a.test.ts"]) == 1
    assert sandbox_check.main(["test", "src/a.test.ts"]) == 1
    capsys.readouterr()

    checks = sandbox_check.collect_checks(7)
    assert [c["kind"] for c in checks] == ["typecheck", "test", "test"]
    assert checks[0] == {"kind": "typecheck", "files": [], "cmd": "pnpm -r typecheck",
                         "exit": 0, "error_kind": None, "error": None,
                         "error_excerpt": None, "duration_s": 12.5, "at": checks[0]["at"]}
    assert checks[0]["at"].startswith("20")
    assert checks[1]["files"] == ["src/a.test.ts"]
    assert checks[1]["exit"] == 20 and checks[1]["error_kind"] is None
    assert checks[1]["error_excerpt"] == "expected 1 to be 2"
    assert checks[2]["exit"] is None
    assert checks[2]["error_kind"] == "infrastructure"
    assert "sandbox lock" in checks[2]["error"]
    # Collecting consumes the file: the next request starts with nothing.
    assert sandbox_check.collect_checks(7) == []


def test_a_refused_run_records_the_refusal_as_its_error(tmp_path, monkeypatch,
                                                        typecheck_profile, capsys):
    _under_author(monkeypatch, tmp_path)
    monkeypatch.setattr(compile_preflight, "run_command_for_patch",
                        lambda pr, head, patch, cmd: {"cmd": cmd, "duration_s": 0.0,
                                                      "refused": "deps touched"})
    assert sandbox_check.main(["typecheck"]) == 1
    capsys.readouterr()
    [c] = sandbox_check.collect_checks(7)
    assert c["error_kind"] == "refused" and c["error"] == "deps touched"


def test_the_preflights_own_error_kind_is_kept(tmp_path, monkeypatch, typecheck_profile,
                                              capsys):
    _under_author(monkeypatch, tmp_path)
    monkeypatch.setattr(compile_preflight, "run_command_for_patch",
                        lambda pr, head, patch, cmd: {"cmd": cmd, "exit": 20, "duration_s": 1.0,
                                                      "error": "the base fails it too",
                                                      "error_kind": "base-compile"})
    sandbox_check.main(["typecheck"])
    capsys.readouterr()
    [c] = sandbox_check.collect_checks(7)
    assert c["error_kind"] == "base-compile"


def test_the_record_is_bounded(tmp_path, monkeypatch, typecheck_profile, capsys):
    _under_author(monkeypatch, tmp_path)
    monkeypatch.setattr(compile_preflight, "run_command_for_patch",
                        lambda pr, head, patch, cmd: {"cmd": cmd, "exit": 20, "duration_s": 1.0,
                                                      "error_excerpt": "x" * 10_000,
                                                      "error": "y" * 10_000})
    sandbox_check.main(["typecheck"])
    capsys.readouterr()
    [c] = sandbox_check.collect_checks(7)
    assert len(c["error_excerpt"]) == sandbox_check.RECORD_CHARS
    assert len(c["error"]) == sandbox_check.RECORD_CHARS


def test_collecting_skips_lines_that_are_not_records_and_caps_the_count(tmp_path,
                                                                        monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "scratch"))
    p = sandbox_check.checks_path(9)
    p.parent.mkdir(parents=True)
    lines = ['{"kind": "typecheck", "exit": 0}'] * (sandbox_check.MAX_CHECKS + 5)
    p.write_text("\n".join(["not json", "[1, 2]", *lines]) + "\n")
    checks = sandbox_check.collect_checks(9)
    assert len(checks) == sandbox_check.MAX_CHECKS
    assert all(c["kind"] == "typecheck" for c in checks)
    assert not p.exists()


def test_a_missing_file_collects_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "scratch"))
    assert sandbox_check.collect_checks(9) == []


def test_discard_drops_a_stale_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "scratch"))
    p = sandbox_check.checks_path(9)
    p.parent.mkdir(parents=True)
    p.write_text('{"kind": "typecheck", "exit": 0}\n')
    sandbox_check.discard_checks(9)
    assert not p.exists()
    sandbox_check.discard_checks(9)
