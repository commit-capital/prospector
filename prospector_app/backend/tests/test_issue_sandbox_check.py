"""The issue-fix lane's one host command: the agent chooses a lane and test
files, the tree it measures and the run cap come from the environment the worker
set, and the sandbox is mocked."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import profile, prove
from prospector_app.backend import issue_sandbox_check, sandbox_check

TYPECHECK = profile.parse_profile(
    {"version": 1, "verify": {"compile_cmd": "pnpm -r typecheck"}}, "t")


def _under_lane(monkeypatch, tmp_path, *, test_patch: str | None = None,
                max_runs: int = 8) -> Path:
    monkeypatch.setattr(profile, "active", lambda: TYPECHECK)
    monkeypatch.setattr(sandbox_check, "authored_patch",
                        lambda wt: "diff --git a/e b/e\n+e\n")
    records = tmp_path / "fix.checks.jsonl"
    monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_ISSUE", "12")
    monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_BASE_SHA", "a" * 40)
    monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_TIER", "2")
    monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_IMAGE", "pr-verify-base:x")
    monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_CLONE", str(tmp_path / "clone"))
    monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_WORKTREE", str(tmp_path / "wt"))
    monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_RECORDS", str(records))
    monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_MAX_RUNS", str(max_runs))
    if test_patch is not None:
        monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_TEST_PATCH", test_patch)
    return records


def _stub_compose(monkeypatch, tmp_path) -> None:
    composed = tmp_path / "composed.patch"
    composed.write_text("diff --git a/x b/x\n+x\n")
    monkeypatch.setattr(prove, "compose", lambda *a, **k: composed)


def test_a_pre_patch_composes_the_tree_the_agent_s_clone_holds(monkeypatch, tmp_path):
    # The agent's diff is against base+pre_patch; the sandbox starts from the
    # base, so the pre-patch leads the composition and flatten (not compose)
    # carries it — the parts touch the same paths.
    pre = tmp_path / "pre.patch"
    pre.write_text("diff --git a/src/app.ts b/src/app.ts\n+pre\n")
    _under_lane(monkeypatch, tmp_path)
    monkeypatch.setenv("PROSPECTOR_ISSUE_CHECK_PRE_PATCH", str(pre))
    flattened = tmp_path / "flat.patch"
    flattened.write_text("diff --git a/x b/x\n+x\n")
    seen: dict = {}

    def fake_flatten(base_clone, *parts, label):
        seen["clone"] = base_clone
        seen["parts"] = parts
        return flattened

    monkeypatch.setattr(prove, "flatten", fake_flatten)
    monkeypatch.setattr(prove, "compose",
                        lambda *a, **k: pytest.fail("compose cannot carry a pre-patch"))
    monkeypatch.setattr(prove, "run_command",
                        lambda base, patch, cmd, *, phase, label: {
                            "cmd": cmd, "exit": 0, "patch": str(patch)})

    assert issue_sandbox_check.main(["typecheck"]) == 0
    assert seen["clone"] == Path(tmp_path / "clone")
    assert seen["parts"][0] == pre.read_text()
    assert seen["parts"][-1] == "diff --git a/e b/e\n+e\n"


def test_missing_env_is_a_usage_error(monkeypatch, capsys):
    for k in ("PROSPECTOR_ISSUE_CHECK_ISSUE", "PROSPECTOR_ISSUE_CHECK_BASE_SHA",
              "PROSPECTOR_ISSUE_CHECK_TIER", "PROSPECTOR_ISSUE_CHECK_IMAGE",
              "PROSPECTOR_ISSUE_CHECK_CLONE", "PROSPECTOR_ISSUE_CHECK_WORKTREE",
              "PROSPECTOR_ISSUE_CHECK_RECORDS", "PROSPECTOR_ISSUE_CHECK_MAX_RUNS"):
        monkeypatch.delenv(k, raising=False)
    assert issue_sandbox_check.main(["typecheck"]) == 2
    assert "PROSPECTOR_ISSUE_CHECK_" in capsys.readouterr().err


def test_typecheck_runs_the_compile_phase_and_test_runs_green(monkeypatch, tmp_path):
    _under_lane(monkeypatch, tmp_path)
    _stub_compose(monkeypatch, tmp_path)
    seen: dict = {}

    def fake_run(base, patch, cmd, *, phase, label):
        seen.update(phase=phase, cmd=cmd)
        return {"cmd": cmd, "exit": 0, "output_tail": "PASS"}
    monkeypatch.setattr(prove, "run_command", fake_run)

    assert issue_sandbox_check.main(["typecheck"]) == 0
    assert seen["phase"] == "compile" and seen["cmd"] == "pnpm -r typecheck"
    assert issue_sandbox_check.main(["test", "a.test.ts"]) == 0
    assert seen["phase"] == "green"
    assert seen["cmd"].startswith("npx vitest run a.test.ts")


def test_a_passing_run_exits_zero_and_a_failing_run_prints_its_tail(monkeypatch,
                                                                    tmp_path, capsys):
    _under_lane(monkeypatch, tmp_path)
    _stub_compose(monkeypatch, tmp_path)
    monkeypatch.setattr(prove, "run_command",
                        lambda *a, **k: {"cmd": "c", "exit": 0, "output_tail": "ok"})
    assert issue_sandbox_check.main(["typecheck"]) == 0
    assert "ok" in capsys.readouterr().out
    monkeypatch.setattr(prove, "run_command",
                        lambda *a, **k: {"cmd": "c", "exit": 20,
                                         "output_tail": "FAIL: boom"})
    assert issue_sandbox_check.main(["typecheck"]) == 1
    assert "FAIL: boom" in capsys.readouterr().out


def test_every_run_is_appended_to_the_records_file(monkeypatch, tmp_path):
    records = _under_lane(monkeypatch, tmp_path)
    _stub_compose(monkeypatch, tmp_path)
    monkeypatch.setattr(prove, "run_command",
                        lambda *a, **k: {"cmd": "pnpm -r typecheck", "exit": 0,
                                         "output_tail": ""})
    assert issue_sandbox_check.main(["typecheck"]) == 0
    lines = records.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "typecheck" and rec["exit"] == 0


def test_past_the_run_cap_the_tool_refuses_without_running(monkeypatch, tmp_path, capsys):
    records = _under_lane(monkeypatch, tmp_path, max_runs=8)
    records.write_text("\n".join(['{"kind": "typecheck", "exit": 0}'] * 8) + "\n")
    _stub_compose(monkeypatch, tmp_path)
    monkeypatch.setattr(prove, "run_command",
                        lambda *a, **k: pytest.fail("run_command called past the cap"))
    assert issue_sandbox_check.main(["typecheck"]) == 1
    assert "check refused" in capsys.readouterr().out
    assert len(records.read_text().splitlines()) == 8


def test_a_compose_conflict_is_refused_and_recorded(monkeypatch, tmp_path, capsys):
    records = _under_lane(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise ValueError("patch parts share paths: ['src/x.ts']")
    monkeypatch.setattr(prove, "compose", boom)
    monkeypatch.setattr(prove, "run_command",
                        lambda *a, **k: pytest.fail("run_command reached after compose failed"))
    assert issue_sandbox_check.main(["typecheck"]) == 1
    assert "check refused" in capsys.readouterr().out
    rec = json.loads(records.read_text().splitlines()[0])
    assert rec["error_kind"] == "refused" and "share paths" in rec["error"]
