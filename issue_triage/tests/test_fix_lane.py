"""The lane core: one issue driven through reproduce -> judge -> fix -> prove ->
review over single-commit clones of the pinned base. Every agent and every
sandbox call is a subprocess/Docker boundary and is mocked; the host validators,
the gates, and the git-backed clone work run for real, so a test proves the
integrator's own wiring — which host exit and which policy name each ending."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_triage import (
    fix_issue,
    fix_lane,
    judge_repro,
    reproduce_issue,
    review_issue_fix,
)
from pipeline import gates, headless_agent, prove, verify_driver


def _keep(worktree: str) -> list[dict]:
    """Write the preservation test a reproduction must carry, returning its entry."""
    p = Path(worktree) / "src" / "keep.test.ts"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("test('keep', () => { expect(1).toBe(1); });\n")
    return [{"path": "src/keep.test.ts", "purpose": "a valid input still works"}]


def _repro_writes_test(worktree: str) -> dict:
    p = Path(worktree) / "src" / "repro.test.ts"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("test('repro', () => { throw new Error('boom'); });\n")
    return {"files": [{"path": "src/repro.test.ts", "purpose": "reproduces the defect"}],
            "preserve": _keep(worktree),
            "claimed_symptom": "throws on empty input",
            "expected_red_signature": "Error: boom",
            "confidence": "high"}


def _judge_reproduced(worktree: str) -> dict:
    return {"symptom_match": {"matches": True, "confidence": "high", "reasoning": "matches"},
            "defect": {"is_defect": True, "confidence": "high", "reasoning": "a defect"}}


def _fix_edits_x(worktree: str) -> dict:
    (Path(worktree) / "src" / "x.ts").write_text("export const x = 2;\n")
    return {"summary": "Guard against empty input",
            "root_cause": "off by one in parse",
            "changes": [{"path": "src/x.ts", "rationale": "return early"}]}


def _review_safe(worktree: str, patch: str, lens: str) -> dict:
    return {"lens": lens, "verdict": "safe", "reason": "tried to break it", "concerns": []}


def _red_confirmed() -> dict:
    return {"exit": gates.SENTINEL_TEST_FAIL, "exit_confirm": gates.SENTINEL_TEST_FAIL,
            "output_tail": "Error: boom\n", "duration_s": 1.0}


def _green_confirmed() -> dict:
    return {"exit": gates.SENTINEL_PASS, "exit_confirm": gates.SENTINEL_PASS,
            "output_tail": "ok\n", "duration_s": 1.0}


@dataclass
class Harness:
    base: prove.PinnedBase
    workdir: Path
    scripts: SimpleNamespace
    calls: dict

    def run(self, *, action: str = "fix", title: str = "Crash on empty",
            body: str = "It throws when given nothing.",
            pre_patch: str | None = None,
            still_valid=None) -> fix_lane.LaneResult:
        spec = fix_lane.LaneSpec(issue=7, title=title, body=body, base=self.base,
                                 action=action, pre_patch=pre_patch)
        return fix_lane.run(spec, workdir=self.workdir,
                            on_step=lambda s: self.calls["steps"].append(s),
                            still_valid=still_valid or (lambda: None))


@pytest.fixture
def lane(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    (base_dir / "src").mkdir(parents=True)
    (base_dir / "src" / "x.ts").write_text("export const x = 1;\n")
    base = prove.PinnedBase(sha="a" * 40, tier=2, image="pr-verify-base:t", clone=base_dir)

    scratch = tmp_path / "vscratch"
    monkeypatch.setattr(verify_driver, "SCRATCH", scratch)
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(scratch))
    monkeypatch.delenv("TRIAGE_PROFILE", raising=False)

    scripts = SimpleNamespace(
        reproduce=_repro_writes_test, judge=_judge_reproduced, fix=_fix_edits_x,
        review=_review_safe, red=_red_confirmed, green=_green_confirmed,
        preserve=_green_confirmed,
        run_command=lambda phase, cmd, patch: {
            "cmd": cmd, "label": "l", "base_sha": base.sha, "output_tail": "ok",
            "exit": gates.SENTINEL_PASS, "duration_s": 1.0})
    calls: dict = {"reproduce": [], "judge": [], "fix": [], "review": [], "red": 0,
                   "green": 0, "preserve": 0, "evidence": [], "run_command": [], "compose": [], "flatten": [], "steps": [],
                   "fix_status": []}

    def fake_reproduce(worktree, *, issue, title, body, env, retry_note=None,
                       on_event=None):
        calls["reproduce"].append({"worktree": worktree, "retry_note": retry_note,
                                   "env": env})
        return scripts.reproduce(worktree)

    def fake_judge(worktree, *, title, body, files, claimed_symptom,
                   expected_red_signature, red_tail, on_event=None):
        calls["judge"].append({"worktree": worktree, "files": files, "red_tail": red_tail})
        return scripts.judge(worktree)

    def fake_fix(worktree, *, issue, title, body, test_paths, preserve_paths, red_tail,
                 withheld_globs, env, on_event=None):
        status = subprocess.run(["git", "-C", worktree, "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        calls["fix_status"].append(status)
        calls["fix"].append({"worktree": worktree, "test_paths": test_paths,
                             "preserve_paths": preserve_paths,
                             "withheld_globs": withheld_globs, "env": env})
        return scripts.fix(worktree)

    def fake_review(worktree, patch, *, lens, title, body, root_cause, test_paths,
                    evidence, on_event=None):
        calls["review"].append(lens)
        calls["evidence"].append(evidence)
        return scripts.review(worktree, patch, lens)

    def fake_red(base_, *, patch, test_cmd, label, tail_bytes=0):
        calls["red"] += 1
        return scripts.red()

    def fake_green(base_, *, patch, test_cmd, label, tail_bytes=0):
        if "keep.test" in test_cmd:
            calls["preserve"] += 1
            return scripts.preserve()
        calls["green"] += 1
        return scripts.green()

    def fake_run_command(base_, patch, cmd, *, phase, label):
        calls["run_command"].append({"phase": phase, "cmd": cmd})
        return scripts.run_command(phase, cmd, patch)

    def fake_compose(label, *parts):
        calls["compose"].append(parts)
        scratch.mkdir(parents=True, exist_ok=True)
        out = scratch / f"{label}.compose.{len(calls['compose'])}.patch"
        out.write_text("".join(p for p in parts if isinstance(p, str)))
        return out

    def fake_flatten(base_clone, *parts, label):
        calls["flatten"].append({"base_clone": base_clone, "parts": parts, "label": label})
        return scratch / f"{label}.flatten.patch"

    monkeypatch.setattr(reproduce_issue, "author", fake_reproduce)
    monkeypatch.setattr(judge_repro, "judge", fake_judge)
    monkeypatch.setattr(fix_issue, "author", fake_fix)
    monkeypatch.setattr(review_issue_fix, "review", fake_review)
    monkeypatch.setattr(prove, "red_legs", fake_red)
    monkeypatch.setattr(prove, "green_legs", fake_green)
    monkeypatch.setattr(prove, "run_command", fake_run_command)
    monkeypatch.setattr(prove, "compose", fake_compose)
    monkeypatch.setattr(prove, "flatten", fake_flatten)
    return Harness(base=base, workdir=tmp_path / "work", scripts=scripts, calls=calls)


# --- report_sha -------------------------------------------------------------


def test_report_sha_is_stable_and_sixteen_hex():
    a = fix_lane.report_sha("A title", "A body")
    b = fix_lane.report_sha("A title", "A body")
    assert a == b
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)
    assert fix_lane.report_sha("A title", "A body") != fix_lane.report_sha("A title", "other")


# --- reproduction endings ---------------------------------------------------


def test_action_reproduce_ends_reproduced(lane):
    res = lane.run(action="reproduce")
    assert res.ending == "reproduced" and res.fault is False
    assert res.reproduction is not None
    assert res.reproduction["outcome"] == "reproduced"
    assert res.reproduction["files"] == [{"path": "src/repro.test.ts",
                                          "contents": "test('repro', () => { throw new Error('boom'); });\n"}]
    assert res.reproduction["report_sha"] == fix_lane.report_sha("Crash on empty",
                                                                 "It throws when given nothing.")
    assert res.reproduction["base_sha"] == "a" * 40
    assert res.result is None
    assert lane.calls["fix"] == []


def test_give_up_ends_unwritable(lane):
    lane.scripts.reproduce = lambda wt: {"give_up": "needs a live browser",
                                         "kind": "needs-live-service"}
    res = lane.run(action="reproduce")
    assert res.ending == "unwritable" and res.fault is False
    assert res.reproduction["give_up"] == "needs a live browser"
    assert lane.calls["red"] == 0


def test_editing_a_tracked_file_ends_unwritable_after_the_retry(lane):
    def repro_and_edit(wt: str) -> dict:
        (Path(wt) / "src" / "repro.test.ts").write_text("test('r', () => {});\n")
        (Path(wt) / "src" / "x.ts").write_text("export const x = 9;\n")
        return {"files": [{"path": "src/repro.test.ts", "purpose": "p"}],
                "preserve": _keep(wt), "claimed_symptom": "s", "expected_red_signature": "sig",
                "confidence": "high"}

    lane.scripts.reproduce = repro_and_edit
    res = lane.run(action="reproduce")
    assert res.ending == "unwritable" and res.fault is False
    assert len(lane.calls["reproduce"]) == 2
    assert lane.calls["reproduce"][1]["retry_note"]


def _with_existing_test(lane) -> Path:
    """An existing test file on the base the reproduction may extend."""
    p = lane.base.clone / "src" / "existing.test.ts"
    p.write_text("test('already here', () => { expect(1).toBe(1); });\n")
    return p


def test_adding_a_case_to_an_existing_test_file_is_written(lane):
    _with_existing_test(lane)

    def repro_extends(wt: str) -> dict:
        p = Path(wt) / "src" / "existing.test.ts"
        p.write_text(p.read_text() + "test('repro', () => { throw new Error('boom'); });\n")
        return {"files": [{"path": "src/existing.test.ts", "purpose": "reproduces it"}],
                "preserve": _keep(wt), "claimed_symptom": "throws", "expected_red_signature": "Error: boom",
                "confidence": "high"}

    lane.scripts.reproduce = repro_extends
    res = lane.run(action="reproduce")
    assert res.ending == "reproduced" and res.fault is False
    assert len(lane.calls["reproduce"]) == 1  # no retry: the set is valid


def test_rewriting_an_existing_test_file_still_ends_unwritable(lane):
    _with_existing_test(lane)

    def repro_rewrites(wt: str) -> dict:
        # The existing case is gone: a removed line is not an addition.
        (Path(wt) / "src" / "existing.test.ts").write_text(
            "test('repro', () => { throw new Error('boom'); });\n")
        return {"files": [{"path": "src/existing.test.ts", "purpose": "reproduces it"}],
                "preserve": _keep(wt), "claimed_symptom": "throws", "expected_red_signature": "Error: boom",
                "confidence": "high"}

    lane.scripts.reproduce = repro_rewrites
    res = lane.run(action="reproduce")
    assert res.ending == "unwritable" and res.fault is False


def test_an_undisclosed_extension_of_an_existing_test_file_is_refused(lane):
    _with_existing_test(lane)

    def repro_extends_quietly(wt: str) -> dict:
        p = Path(wt) / "src" / "existing.test.ts"
        p.write_text(p.read_text() + "test('extra', () => {});\n")
        (Path(wt) / "src" / "repro.test.ts").write_text("test('r', () => {});\n")
        return {"files": [{"path": "src/repro.test.ts", "purpose": "p"}],
                "preserve": _keep(wt), "claimed_symptom": "s",
                "expected_red_signature": "sig", "confidence": "high"}

    lane.scripts.reproduce = repro_extends_quietly
    res = lane.run(action="reproduce")
    assert res.ending == "unwritable" and res.fault is False


def test_first_red_leg_passing_ends_not_reproduced_after_the_retry(lane):
    lane.scripts.red = lambda: {"exit": gates.SENTINEL_PASS, "exit_confirm": None,
                                "output_tail": "", "duration_s": 1.0}
    res = lane.run(action="reproduce")
    assert res.ending == "not-reproduced" and res.fault is False
    assert len(lane.calls["reproduce"]) == 2
    assert lane.calls["red"] == 2


def test_a_wrong_symptom_rating_ends_wrong_symptom(lane):
    lane.scripts.judge = lambda wt: {
        "symptom_match": {"matches": False, "confidence": "high", "reasoning": "no"},
        "defect": {"is_defect": True, "confidence": "high", "reasoning": "y"}}
    res = lane.run(action="reproduce")
    assert res.ending == "wrong-symptom" and res.fault is False


def test_a_not_a_defect_rating_ends_not_a_defect(lane):
    lane.scripts.judge = lambda wt: {
        "symptom_match": {"matches": True, "confidence": "high", "reasoning": "y"},
        "defect": {"is_defect": False, "confidence": "high", "reasoning": "intended"}}
    res = lane.run(action="reproduce")
    assert res.ending == "not-a-defect" and res.fault is False


# --- fix endings ------------------------------------------------------------


def test_the_happy_path_ends_fixed(lane):
    res = lane.run()
    assert res.ending == "fixed" and res.fault is False
    assert res.reproduction["outcome"] == "reproduced"
    assert res.result is not None
    assert res.result["summary"] == "Guard against empty input"
    assert res.result["root_cause"] == "off by one in parse"
    assert res.result["changes"] == [{"path": "src/x.ts", "rationale": "return early"}]
    assert res.result["tier"]["tier"] == 2
    assert [r["lens"] for r in res.result["reviews"]] == ["root-cause", "scope-safety"]
    assert res.result["proof"]["red"]["exit"] == gates.SENTINEL_TEST_FAIL
    assert res.result["proof"]["green"]["exit"] == gates.SENTINEL_PASS
    assert res.agent_runs == 5


def test_the_fix_clone_carries_the_frozen_test_committed(lane):
    lane.run()
    # The fix agent is handed a clone whose only commit already holds the frozen
    # reproduction test, so it opens on a clean working tree.
    assert lane.calls["fix_status"] == [""]
    assert lane.calls["fix"][0]["test_paths"] == ["src/repro.test.ts"]


def test_fix_giving_up_ends_no_fix(lane):
    lane.scripts.fix = lambda wt: {"give_up": "needs product judgment"}
    res = lane.run()
    assert res.ending == "no-fix" and res.fault is False
    assert lane.calls["green"] == 0


def test_a_fix_touching_a_test_file_ends_fix_untrusted(lane):
    def fix_touches_test(wt: str) -> dict:
        (Path(wt) / "src" / "x.ts").write_text("export const x = 2;\n")
        (Path(wt) / "src" / "extra.test.ts").write_text("test('e', () => {});\n")
        return {"summary": "s", "root_cause": "rc",
                "changes": [{"path": "src/x.ts", "rationale": "a"},
                            {"path": "src/extra.test.ts", "rationale": "b"}]}

    lane.scripts.fix = fix_touches_test
    res = lane.run()
    assert res.ending == "fix-untrusted" and res.fault is False
    assert "test" in res.detail
    assert lane.calls["green"] == 0


def test_green_failing_ends_fix_unproven(lane):
    lane.scripts.green = lambda: {"exit": gates.SENTINEL_TEST_FAIL, "exit_confirm": None,
                                  "output_tail": "still red\n", "duration_s": 1.0}
    res = lane.run()
    assert res.ending == "fix-unproven" and res.fault is False


def test_a_root_cause_rejection_ends_fix_rejected_and_skips_scope_safety(lane):
    def review(wt, patch, lens):
        if lens == "root-cause":
            return {"lens": lens, "verdict": "unsafe", "reason": "special-cases the input",
                    "concerns": ["hard-codes the return"]}
        return _review_safe(wt, patch, lens)

    lane.scripts.review = review
    res = lane.run()
    assert res.ending == "fix-rejected" and res.fault is False
    assert lane.calls["review"] == ["root-cause"]
    assert "root-cause" in res.detail


# --- cancel -----------------------------------------------------------------


def test_a_report_edited_cancel_before_the_fix_stage_ends_cancelled(lane):
    res = lane.run(still_valid=lambda: "report-edited")
    assert res.ending == "cancelled" and res.fault is False
    assert res.detail == "report-edited"
    assert res.reproduction is not None
    assert lane.calls["fix"] == []


# --- faults -----------------------------------------------------------------


def test_an_agent_outage_ends_agent_unavailable(lane):
    def outage(wt):
        raise headless_agent.AgentUnavailable("not logged in")

    lane.scripts.reproduce = outage
    res = lane.run()
    assert res.ending == "agent-unavailable" and res.fault is True
    assert res.agent_runs == 1


def test_a_declined_prompt_ends_declined_without_fault(lane):
    def declined(wt):
        raise headless_agent.AgentDeclined("safeguards flagged this message")

    lane.scripts.reproduce = declined
    res = lane.run()
    assert res.ending == "declined" and res.fault is False


def test_a_failed_judge_ends_run_failed(lane):
    lane.scripts.judge = lambda wt: {"failed": True, "reason": "the judge crashed"}
    res = lane.run()
    assert res.ending == "run-failed" and res.fault is True


def test_a_review_failure_with_no_rejection_ends_run_failed(lane):
    def review(wt, patch, lens):
        if lens == "scope-safety":
            return {"lens": lens, "verdict": "unsafe", "reason": "reviewer crashed",
                    "concerns": [], "failed": True}
        return _review_safe(wt, patch, lens)

    lane.scripts.review = review
    res = lane.run()
    assert res.ending == "run-failed" and res.fault is True


def test_a_non_sentinel_red_exit_ends_sandbox(lane):
    lane.scripts.red = lambda: {"exit": 124, "exit_confirm": None,
                                "output_tail": "timeout\n", "duration_s": 1.0}
    res = lane.run()
    assert res.ending == "sandbox" and res.fault is True


def test_a_probe_failure_ends_sandbox(lane):
    def raise_probe():
        raise verify_driver.ProbeFailure("isolation unproven")

    lane.scripts.red = raise_probe
    res = lane.run()
    assert res.ending == "sandbox" and res.fault is True


def test_a_base_compile_failure_ends_base_compile(lane, monkeypatch):
    from pipeline import profile
    configured = profile.parse_profile(
        {"version": 1, "verify": {"compile_cmd": "tsc --noEmit"}}, "t")
    monkeypatch.setattr(profile, "active", lambda: configured)

    def run_command(phase, cmd, patch):
        if phase == "compile":
            return {"cmd": cmd, "exit": gates.SENTINEL_TEST_FAIL,
                    "error": "base fails: at aaaaaaaaaaaa: tsc broke",
                    "error_kind": "base-compile", "output_tail": "", "duration_s": 1.0}
        return {"cmd": cmd, "exit": gates.SENTINEL_PASS, "output_tail": "ok", "duration_s": 1.0}

    lane.scripts.run_command = run_command
    res = lane.run()
    assert res.ending == "base-compile" and res.fault is True
    assert res.result["proof"]["compile"]["error_kind"] == "base-compile"


# --- structural -------------------------------------------------------------


def test_the_clones_are_removed_afterward(lane):
    lane.run()
    assert not (lane.workdir / "repro").exists()
    assert not (lane.workdir / "fix").exists()


def test_the_clones_are_removed_after_a_fault(lane):
    lane.scripts.judge = lambda wt: {"failed": True, "reason": "boom"}
    lane.run()
    assert not (lane.workdir / "repro").exists()
    assert not (lane.workdir / "fix").exists()


def test_agent_runs_counts_every_agent_invocation(lane):
    # reproduce + judge + fix + root-cause + scope-safety.
    res = lane.run()
    assert res.agent_runs == 5


def test_the_happy_path_emits_its_steps_in_order(lane):
    lane.run()
    steps = lane.calls["steps"]
    assert steps[:5] == ["preparing the clone", "agent authoring the reproduction",
                         "proving red on the pinned base",
                         "proving the preservation tests pass on the pinned base",
                         "judging the reproduction"]
    assert (steps.index("proving green")
            < steps.index("proving the preservation tests still pass"))
    assert "agent authoring the fix" in steps
    assert "re-gating the fix" in steps
    assert "proving green" in steps
    assert steps.index("reviewing: root-cause") < steps.index("reviewing: scope-safety")


def test_a_passing_compile_continues_to_review_and_fixes(lane, monkeypatch):
    from pipeline import profile
    configured = profile.parse_profile(
        {"version": 1, "verify": {"compile_cmd": "tsc --noEmit"}}, "t")
    monkeypatch.setattr(profile, "active", lambda: configured)
    res = lane.run()
    assert res.ending == "fixed" and res.fault is False
    assert res.result["proof"]["compile"]["exit"] == gates.SENTINEL_PASS
    assert [r["lens"] for r in res.result["reviews"]] == ["root-cause", "scope-safety"]


def test_related_tests_the_base_also_fails_do_not_sink_the_fix(lane, monkeypatch):
    from pipeline import resolve_evidence
    monkeypatch.setattr(resolve_evidence, "related_tests",
                        lambda wt, paths: ["src/other.test.ts"])
    monkeypatch.setattr(verify_driver, "derive_test_command",
                        lambda paths: "npx vitest run " + " ".join(paths))

    def run_command(phase, cmd, patch):
        # Both the related-tests run and its base re-run fail, so the failure
        # predates the fix and must not count against it.
        if "other.test.ts" in cmd:
            return {"cmd": cmd, "exit": gates.SENTINEL_TEST_FAIL, "output_tail": "fail",
                    "duration_s": 1.0}
        return {"cmd": cmd, "exit": gates.SENTINEL_PASS, "output_tail": "ok", "duration_s": 1.0}

    lane.scripts.run_command = run_command
    res = lane.run()
    assert res.ending == "fixed" and res.fault is False
    entry = res.result["proof"]["related_tests"]
    assert entry["files"] == ["src/other.test.ts"]
    assert entry["base_fails"] is True


def test_a_review_stage_outage_ends_agent_unavailable(lane):
    def outage(wt, patch, lens):
        raise headless_agent.AgentUnavailable("not logged in")

    lane.scripts.review = outage
    res = lane.run()
    assert res.ending == "agent-unavailable" and res.fault is True


# --- pre_patch (history replay) ----------------------------------------------

# A real, applying diff: materialize is not mocked, so a pre_patch must be one
# git can actually apply to the fixture base's src/x.ts.
_PRE_PATCH = (
    "diff --git a/src/x.ts b/src/x.ts\n"
    "index e69de29..0000000 100644\n"
    "--- a/src/x.ts\n"
    "+++ b/src/x.ts\n"
    "@@ -1 +1 @@\n"
    "-export const x = 1;\n"
    "+export const x = 42;\n"
)


def test_pre_patch_routes_the_red_and_green_legs_through_flatten(lane):
    res = lane.run(pre_patch=_PRE_PATCH)
    assert res.ending == "fixed" and res.fault is False
    # compose holds only the authored tests; every proof leg flattens.
    assert len(lane.calls["compose"]) == 1
    # red, the preservation tests on the base, green, the preservation tests
    # with the fix.
    assert len(lane.calls["flatten"]) == 4
    for call in lane.calls["flatten"]:
        assert call["base_clone"] == lane.base.clone
        assert call["parts"][0] == _PRE_PATCH


def test_pre_patch_is_applied_before_the_agent_sees_the_clone(lane):
    seen: dict[str, str] = {}

    def reproduce_and_capture(worktree: str) -> dict:
        seen["repro"] = (Path(worktree) / "src" / "x.ts").read_text()
        return _repro_writes_test(worktree)

    def fix_and_capture(worktree: str) -> dict:
        seen["fix"] = (Path(worktree) / "src" / "x.ts").read_text()
        return _fix_edits_x(worktree)

    lane.scripts.reproduce = reproduce_and_capture
    lane.scripts.fix = fix_and_capture
    res = lane.run(pre_patch=_PRE_PATCH)
    assert res.ending == "fixed" and res.fault is False
    assert seen == {"repro": "export const x = 42;\n", "fix": "export const x = 42;\n"}


def test_pre_patch_reaches_the_agent_s_check_environment(lane):
    # The agents author against base+pre_patch, so their one host check must
    # compose the same tree; the sandbox itself starts from the base.
    res = lane.run(pre_patch=_PRE_PATCH)
    assert res.ending == "fixed" and res.fault is False
    named = [call["env"]["PROSPECTOR_ISSUE_CHECK_PRE_PATCH"]
             for call in lane.calls["reproduce"] + lane.calls["fix"]]
    assert named and len(set(named)) == 1
    assert Path(named[0]).read_text() == _PRE_PATCH


def test_without_a_pre_patch_the_check_environment_names_none(lane):
    lane.run()
    for call in lane.calls["reproduce"] + lane.calls["fix"]:
        assert "PROSPECTOR_ISSUE_CHECK_PRE_PATCH" not in call["env"]


def test_pre_patch_none_uses_compose_not_flatten(lane):
    res = lane.run()
    assert res.ending == "fixed" and res.fault is False
    assert lane.calls["flatten"] == []
    # the authored tests, then red, preservation on the base, green, preservation
    # with the fix.
    assert len(lane.calls["compose"]) == 5


# --- preservation tests ------------------------------------------------------


def _repro_without_preservation(wt: str) -> dict:
    out = _repro_writes_test(wt)
    (Path(wt) / "src" / "keep.test.ts").unlink()
    out["preserve"] = []
    return out


def test_a_reproduction_without_preservation_tests_ends_unwritable(lane):
    lane.scripts.reproduce = _repro_without_preservation
    res = lane.run()
    assert res.ending == "unwritable" and res.fault is False
    assert lane.calls["reproduce"][1]["retry_note"] == "no-preservation-tests"
    assert lane.calls["red"] == 0


def test_a_preservation_test_that_is_also_the_reproduction_is_refused(lane):
    def overlapping(wt: str) -> dict:
        out = _repro_writes_test(wt)
        out["preserve"] = out["preserve"] + out["files"]
        return out

    lane.scripts.reproduce = overlapping
    res = lane.run()
    assert res.ending == "unwritable"
    assert res.detail == "preservation-overlaps-reproduction"


def test_preservation_tests_failing_on_the_base_are_retried_then_unwritable(lane):
    lane.scripts.preserve = lambda: {"exit": gates.SENTINEL_TEST_FAIL, "exit_confirm": None,
                                     "output_tail": "fail\n", "duration_s": 1.0}
    res = lane.run()
    assert res.ending == "unwritable" and res.fault is False
    assert len(lane.calls["reproduce"]) == 2
    assert lane.calls["reproduce"][1]["retry_note"] == (
        "the preservation tests fail on the pinned base")
    assert lane.calls["judge"] == []


def test_preservation_tests_that_pass_on_a_retry_go_on_to_the_fix(lane):
    runs = iter([{"exit": gates.SENTINEL_TEST_FAIL, "exit_confirm": None,
                  "output_tail": "fail\n", "duration_s": 1.0}])
    lane.scripts.preserve = lambda: next(runs, _green_confirmed())
    res = lane.run()
    assert res.ending == "fixed"
    assert len(lane.calls["reproduce"]) == 2


def test_a_preservation_run_with_no_test_verdict_ends_sandbox(lane):
    lane.scripts.preserve = lambda: {"exit": 124, "exit_confirm": None,
                                     "output_tail": "timeout\n", "duration_s": 1.0}
    res = lane.run()
    assert res.ending == "sandbox" and res.fault is True


def test_a_fix_that_breaks_a_preservation_test_ends_fix_unproven(lane):
    runs = iter([_green_confirmed()])  # the base run passes; the fixed run does not
    lane.scripts.preserve = lambda: next(runs, {
        "exit": gates.SENTINEL_TEST_FAIL, "exit_confirm": None,
        "output_tail": "empty id now 400\n", "duration_s": 1.0})
    res = lane.run()
    assert res.ending == "fix-unproven" and res.fault is False
    assert "preservation" in res.detail
    assert res.result["proof"]["preserve"]["exit"] == gates.SENTINEL_TEST_FAIL


def test_the_fix_agent_and_its_clone_carry_the_preservation_tests(lane):
    seen: dict = {}

    def fix_and_look(wt: str) -> dict:
        seen["kept"] = (Path(wt) / "src" / "keep.test.ts").exists()
        return _fix_edits_x(wt)

    lane.scripts.fix = fix_and_look
    res = lane.run()
    assert res.ending == "fixed"
    assert seen["kept"] is True
    assert lane.calls["fix"][0]["preserve_paths"] == ["src/keep.test.ts"]
    assert res.reproduction["preserve"][0]["path"] == "src/keep.test.ts"
    assert res.reproduction["preserve_on_base"]["exit"] == gates.SENTINEL_PASS


def test_the_reviewers_are_told_what_the_host_observed(lane):
    lane.run()
    assert len(lane.calls["evidence"]) == 2
    evidence = lane.calls["evidence"][1]
    assert "Preservation tests (src/keep.test.ts)" in evidence
    assert "passed twice" in evidence


# --- one run's scratch is its own ----------------------------------------------


def test_each_stage_records_its_checks_inside_the_run_s_workdir(lane):
    lane.run()
    records = [c["env"]["PROSPECTOR_ISSUE_CHECK_RECORDS"]
               for c in lane.calls["reproduce"] + lane.calls["fix"]]
    assert records == [str(lane.workdir / "repro.checks.jsonl"),
                       str(lane.workdir / "fix.checks.jsonl")]


def test_the_authored_tests_travel_as_a_composed_patch_of_their_own_text(lane):
    lane.run()
    authored = lane.calls["compose"][0]
    assert len(authored) == 1
    assert "src/repro.test.ts" in authored[0] and "src/keep.test.ts" in authored[0]


# --- the full suite ------------------------------------------------------------


def _suite(confirmed: bool, flake: bool = False) -> dict:
    return {"exit": 20 if confirmed or flake else 0, "exit_confirm": 20 if confirmed else None,
            "confirmed": confirmed, "flake": flake, "excluded": 3,
            "new_failures": ["server/src/__tests__/other.test.ts"] if confirmed else []}


def test_a_fix_that_breaks_the_full_suite_ends_fix_unproven(lane, monkeypatch):
    seen: list = []

    def suite_proof(spec, patch, label):
        seen.append(patch)
        return _suite(confirmed=True)

    monkeypatch.setattr(fix_lane, "suite_proof", suite_proof)
    res = lane.run()
    assert res.ending == "fix-unproven" and res.fault is False
    assert "other.test.ts" in res.detail
    assert "src/x.ts" in seen[0] and "src/repro.test.ts" in seen[0]


def test_a_suite_flake_does_not_sink_the_fix(lane, monkeypatch):
    monkeypatch.setattr(fix_lane, "suite_proof", lambda spec, patch, label: _suite(False, True))
    res = lane.run()
    assert res.ending == "fixed"
    assert res.result["proof"]["suite"]["flake"] is True


def test_a_suite_that_cannot_complete_is_a_sandbox_fault(lane, monkeypatch):
    def fault(spec, patch, label):
        raise prove.SuiteFault("the suite run did not complete (exit 124)")

    monkeypatch.setattr(fix_lane, "suite_proof", fault)
    res = lane.run()
    assert res.ending == "sandbox" and res.fault is True


def test_the_suite_is_skipped_where_the_profile_names_no_suite(lane):
    res = lane.run()
    assert res.ending == "fixed"
    assert "suite" not in res.result["proof"]


def test_a_tree_that_cannot_plan_a_suite_records_the_skip(lane, monkeypatch):
    monkeypatch.setattr(fix_lane, "suite_proof",
                        lambda spec, patch, label: "this tree's suite wrapper cannot "
                                                         "derive a plan")
    res = lane.run()
    assert res.ending == "fixed"
    assert res.result["proof"]["suite"] == {
        "skipped": "this tree's suite wrapper cannot derive a plan"}
