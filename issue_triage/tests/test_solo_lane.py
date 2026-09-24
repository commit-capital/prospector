"""The one-agent lane: one agent reproduces and fixes; the host's re-gate and
test runs name the ending. The agent and every sandbox call are mocked; the
clone, the re-gate and the patch splitting run for real."""
from __future__ import annotations

from pathlib import Path

import pytest

from issue_triage import fix_lane, solo_lane
from pipeline import gates, prove, resolve_evidence, verify_driver


def _legs(exit_: int, confirm: int | None) -> dict:
    return {"exit": exit_, "exit_confirm": confirm, "output_tail": "", "duration_s": 1.0}


RED = _legs(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL)
GREEN = _legs(gates.SENTINEL_PASS, gates.SENTINEL_PASS)


def _writes_test_and_fix(wt: str) -> dict:
    (Path(wt) / "src" / "bug.test.ts").write_text("test('bug', () => {});\n")
    (Path(wt) / "src" / "x.ts").write_text("export const x = 2;\n")
    return {"summary": "Fix x", "root_cause": "x was 1", "tests": ["src/bug.test.ts"],
            "changes": [{"path": "src/x.ts", "rationale": "set it to 2"}]}


@pytest.fixture
def solo(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    (base_dir / "src").mkdir(parents=True)
    (base_dir / "src" / "x.ts").write_text("export const x = 1;\n")
    (base_dir / "src" / "old.test.ts").write_text("test('old', () => {});\n")
    base = prove.PinnedBase(sha="a" * 40, tier=2, image="img", clone=base_dir)
    scratch = tmp_path / "vscratch"
    monkeypatch.setattr(verify_driver, "SCRATCH", scratch)
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(scratch))
    monkeypatch.delenv("TRIAGE_PROFILE", raising=False)
    calls: dict = {"agent": _writes_test_and_fix, "red": RED, "green": GREEN,
                   "related": None, "env": None, "commands": []}

    def fake_author(worktree, *, title, body, env, on_event=None):
        calls["env"] = env
        return calls["agent"](worktree)

    def fake_compose(label, *parts):
        scratch.mkdir(parents=True, exist_ok=True)
        out = scratch / f"{label}.{len(calls['commands'])}.patch"
        out.write_text("".join(parts))
        return out

    def fake_run_command(base_, patch, cmd, *, phase, label):
        calls["commands"].append(cmd)
        exit_ = calls["related"] if calls["related"] is not None else gates.SENTINEL_PASS
        return {"cmd": cmd, "exit": exit_, "output_tail": "", "duration_s": 1.0}

    monkeypatch.setattr(solo_lane, "author", fake_author)
    monkeypatch.setattr(prove, "compose", fake_compose)
    monkeypatch.setattr(prove, "red_legs", lambda base_, **kw: calls["red"])
    monkeypatch.setattr(prove, "green_legs", lambda base_, **kw: calls["green"])
    monkeypatch.setattr(prove, "run_command", fake_run_command)
    monkeypatch.setattr(resolve_evidence, "related_tests", lambda wt, paths: [])

    def run() -> fix_lane.LaneResult:
        spec = fix_lane.LaneSpec(issue=7, title="x is wrong", body="x should be 2", base=base)
        return solo_lane.run(spec, workdir=tmp_path / "work")

    calls["run"] = run
    calls["workdir"] = tmp_path / "work"
    return calls


def test_a_proven_test_and_fix_end_fixed(solo):
    res = solo["run"]()
    assert res.ending == "fixed" and res.fault is False
    assert res.agent_runs == 1
    assert res.reproduction["outcome"] == "reproduced"
    assert res.reproduction["files"] == [{"path": "src/bug.test.ts"}]
    assert "src/x.ts" in res.result["patch"] and "src/bug.test.ts" in res.result["patch"]
    assert res.result["reviews"] == []


def test_the_agent_gets_the_whole_job_s_run_budget(solo):
    solo["run"]()
    assert solo["env"]["PROSPECTOR_ISSUE_CHECK_MAX_RUNS"] == str(solo_lane.MAX_RUNS)
    assert solo["env"]["PROSPECTOR_ISSUE_CHECK_RECORDS"] == str(
        solo["workdir"] / "solo.checks.jsonl")


@pytest.mark.parametrize("kind,ending", [("not-a-defect", "not-a-defect"),
                                         ("cannot-reproduce", "no-fix")])
def test_a_give_up_ends_by_its_kind(solo, kind, ending):
    solo["agent"] = lambda wt: {"give_up": "no", "kind": kind}
    assert solo["run"]().ending == ending


def test_a_test_that_is_not_green_with_the_fix_ends_fix_unproven(solo):
    solo["green"] = _legs(gates.SENTINEL_TEST_FAIL, None)
    assert solo["run"]().ending == "fix-unproven"


def test_a_test_that_passes_on_the_base_is_recorded_not_gated(solo):
    solo["red"] = _legs(gates.SENTINEL_PASS, None)
    res = solo["run"]()
    assert res.ending == "fixed"
    assert res.reproduction["outcome"] == "not-reproduced"


def test_rewriting_an_existing_test_ends_fix_untrusted(solo):
    def rewrites(wt: str) -> dict:
        out = _writes_test_and_fix(wt)
        (Path(wt) / "src" / "old.test.ts").write_text("test('changed', () => {});\n")
        return out

    solo["agent"] = rewrites
    res = solo["run"]()
    assert res.ending == "fix-untrusted" and "src/old.test.ts" in res.detail


def test_a_change_to_tests_alone_ends_fix_untrusted(solo):
    def tests_only(wt: str) -> dict:
        (Path(wt) / "src" / "bug.test.ts").write_text("test('bug', () => {});\n")
        return {"summary": "s", "root_cause": "r", "tests": ["src/bug.test.ts"], "changes": []}

    solo["agent"] = tests_only
    assert solo["run"]().ending == "fix-untrusted"


def test_failing_related_tests_the_base_passes_end_fix_unproven(solo, monkeypatch):
    monkeypatch.setattr(resolve_evidence, "related_tests", lambda wt, paths: ["src/old.test.ts"])
    solo["related"] = gates.SENTINEL_TEST_FAIL
    calls = {"n": 0}
    real = prove.run_command

    def related_fails_then_base_passes(base_, patch, cmd, *, phase, label):
        calls["n"] += 1
        out = real(base_, patch, cmd, phase=phase, label=label)
        if calls["n"] == 2:
            out["exit"] = gates.SENTINEL_PASS
        return out

    monkeypatch.setattr(prove, "run_command", related_fails_then_base_passes)
    assert solo["run"]().ending == "fix-unproven"


def test_a_fix_with_no_test_of_its_own_rests_on_the_host_s_other_checks(solo):
    def fix_only(wt: str) -> dict:
        (Path(wt) / "src" / "x.ts").write_text("export const x = 2;\n")
        return {"summary": "s", "root_cause": "r", "tests": [],
                "changes": [{"path": "src/x.ts", "rationale": "r"}]}

    solo["agent"] = fix_only
    res = solo["run"]()
    assert res.ending == "fixed" and "no test of its own" in res.detail
    assert res.reproduction["outcome"] is None


def test_the_clone_is_removed_afterward(solo):
    solo["run"]()
    assert not (solo["workdir"] / "solo").exists()


def test_a_solo_fix_that_breaks_the_full_suite_ends_fix_unproven(solo, monkeypatch):
    monkeypatch.setattr(fix_lane, "suite_proof", lambda spec, patch, label: {
        "exit": 20, "exit_confirm": 20, "confirmed": True, "flake": False, "excluded": 0,
        "new_failures": ["src/old.test.ts"]})
    res = solo["run"]()
    assert res.ending == "fix-unproven" and "src/old.test.ts" in res.detail


def test_a_solo_suite_fault_is_a_sandbox_fault(solo, monkeypatch):
    def fault(spec, patch, label):
        raise prove.SuiteFault("no trailer")

    monkeypatch.setattr(fix_lane, "suite_proof", fault)
    assert solo["run"]().ending == "sandbox"
