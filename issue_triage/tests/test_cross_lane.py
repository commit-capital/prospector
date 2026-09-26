"""The cross-tested lane: independent candidates reproduce and fix; every fix
runs against every reproduction, and only an agreed fix is judged further. The
agents and the sandbox are mocked — the sandbox passes a pairing of a test and
a fix by a table — while the clones, the re-gate and the patch splitting run
for real."""
from __future__ import annotations

from pathlib import Path

import pytest

from issue_triage import cross_lane, fix_lane, review_issue_fix, solo_lane
from pipeline import gates, headless_agent, prove, resolve_evidence, verify_driver


def _legs(exit_: int, confirm: int | None) -> dict:
    return {"exit": exit_, "exit_confirm": confirm, "output_tail": "", "duration_s": 1.0}


RED = _legs(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL)
GREEN = _legs(gates.SENTINEL_PASS, gates.SENTINEL_PASS)
FAILS = _legs(gates.SENTINEL_TEST_FAIL, None)


def _writes(test: str, value: int, extra_lines: int = 0):
    """An agent that writes the test `test` (expecting x == `value`) and sets x."""
    def author(wt: str) -> dict:
        (Path(wt) / "src" / test).write_text(f"test('x', () => expect(x).toBe({value}));\n")
        body = f"export const x = {value};\n" + "// pad\n" * extra_lines
        (Path(wt) / "src" / "x.ts").write_text(body)
        return {"summary": "Fix x", "root_cause": "x", "tests": [f"src/{test}"],
                "changes": [{"path": "src/x.ts", "rationale": "set x"}]}
    return author


@pytest.fixture
def cross(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    (base_dir / "src").mkdir(parents=True)
    (base_dir / "src" / "x.ts").write_text("export const x = 1;\n")
    base = prove.PinnedBase(sha="a" * 40, tier=2, image="img", clone=base_dir)
    scratch = tmp_path / "vscratch"
    monkeypatch.setattr(verify_driver, "SCRATCH", scratch)
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(scratch))
    monkeypatch.delenv("TRIAGE_PROFILE", raising=False)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_MODELS", "opus,sonnet,opus")
    state: dict = {"agents": [_writes("a.test.ts", 2), _writes("b.test.ts", 2),
                              _writes("c.test.ts", 2, extra_lines=3)],
                   "models": [], "green_runs": 0}

    def fake_author(worktree, *, title, body, env, model=None, on_event=None):
        index = int(Path(worktree).parent.name.split("-")[1])
        state["models"].append((index, model))
        return state["agents"][index](worktree)

    n = {"i": 0}

    def fake_compose(label, *parts):
        n["i"] += 1
        out = scratch / f"{label}.{n['i']}.patch"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(parts))
        return out

    def expected(text: str) -> int | None:
        for line in text.splitlines():
            if "toBe(" in line:
                return int(line.split("toBe(")[1].split(")")[0])
        return None

    def fixed_value(text: str) -> int:
        for line in text.splitlines():
            if line.startswith("+export const x = "):
                return int(line.split("= ")[1].rstrip(";"))
        return 1

    def fake_red(base_, *, patch, test_cmd, label, tail_bytes=0):
        return RED if expected(patch.read_text()) != 1 else GREEN

    def fake_green(base_, *, patch, test_cmd, label, tail_bytes=0):
        state["green_runs"] += 1
        text = patch.read_text()
        return GREEN if expected(text) == fixed_value(text) else FAILS

    monkeypatch.setattr(solo_lane, "author", fake_author)
    monkeypatch.setattr(prove, "compose", fake_compose)
    monkeypatch.setattr(prove, "red_legs", fake_red)
    monkeypatch.setattr(prove, "green_legs", fake_green)
    monkeypatch.setattr(prove, "run_command", lambda *a, **k: {"exit": 0, "output_tail": ""})
    monkeypatch.setattr(resolve_evidence, "related_tests", lambda wt, paths: [])
    state["review"] = {"lens": "scope-safety", "verdict": "safe", "reason": "only x",
                       "concerns": []}
    state["reviewed"] = []

    def fake_review(worktree, patch, **kw):
        state["reviewed"].append({"worktree": worktree, "patch": patch, **kw})
        assert (Path(worktree) / "src" / "x.ts").read_text().startswith("export const x = 2")
        return state["review"]

    monkeypatch.setattr(review_issue_fix, "review", fake_review)

    def run() -> fix_lane.LaneResult:
        spec = fix_lane.LaneSpec(issue=7, title="x is wrong", body="x should be 2", base=base)
        return cross_lane.run(spec, workdir=tmp_path / "work")

    state["run"] = run
    state["workdir"] = tmp_path / "work"
    return state


def test_candidates_that_agree_end_fixed_with_the_smallest_fix(cross):
    res = cross["run"]()
    assert res.ending == "fixed" and res.fault is False
    assert res.agent_runs == 4
    assert res.result["pick"] in (0, 1)  # candidate 2's fix is the largest
    pick = res.result["pick"]
    assert res.result["agreement"] == {"reproductions": [0, 1, 2], "agreed": [0, 1, 2],
                                       "shipped": [pick] + [i for i in (0, 1, 2) if i != pick]}
    assert "src/x.ts" in res.result["patch"]
    for test in ("a", "b", "c"):
        assert f"src/{test}.test.ts" in res.result["patch"]
    assert [f["path"] for f in res.reproduction["files"]][0] == f"src/{'ab'[pick]}.test.ts"


def test_a_reproduction_writing_a_file_another_ships_is_left_out(cross):
    cross["agents"][2] = _writes("a.test.ts", 2, extra_lines=3)
    res = cross["run"]()
    assert res.ending == "fixed"
    assert sorted(res.result["agreement"]["shipped"]) == [0, 1]
    assert res.result["patch"].count("+++ b/src/a.test.ts") == 1


def test_every_candidate_s_patches_are_kept_even_when_disputed(cross):
    cross["agents"] = [_writes("a.test.ts", 2), _writes("b.test.ts", 3),
                       _writes("c.test.ts", 4)]
    res = cross["run"]()
    kept = res.result["candidate_patches"]
    assert [c["index"] for c in kept] == [0, 1, 2]
    assert all("src/x.ts" in c["fix_patch"] and ".test.ts" in c["test_patch"] for c in kept)


def test_each_candidate_runs_on_its_own_model(cross):
    cross["run"]()
    assert sorted(cross["models"]) == [(0, "opus"), (1, "sonnet"), (2, "opus")]


def test_readings_that_pin_different_behavior_end_fix_disputed(cross):
    cross["agents"] = [_writes("a.test.ts", 2), _writes("b.test.ts", 3),
                       _writes("c.test.ts", 4)]
    res = cross["run"]()
    assert res.ending == "fix-disputed" and res.fault is False
    assert res.result["agreement"]["agreed"] == []


def test_a_majority_reading_does_not_outvote_a_reproduction_it_fails(cross):
    cross["agents"] = [_writes("a.test.ts", 2), _writes("b.test.ts", 2),
                       _writes("c.test.ts", 3)]
    res = cross["run"]()
    assert res.ending == "fix-disputed"


def test_a_single_reproduction_cannot_be_agreed_on(cross):
    def no_test(wt: str) -> dict:
        (Path(wt) / "src" / "x.ts").write_text("export const x = 2;\n")
        return {"summary": "s", "root_cause": "r", "tests": [],
                "changes": [{"path": "src/x.ts", "rationale": "r"}]}

    cross["agents"] = [_writes("a.test.ts", 2), no_test, no_test]
    res = cross["run"]()
    assert res.ending == "fix-unproven" and "1 independent reproduction" in res.detail


def test_an_untrusted_candidate_drops_out_and_the_others_can_still_agree(cross):
    def rewrites_x_only_in_a_test(wt: str) -> dict:
        (Path(wt) / "src" / "c.test.ts").write_text("test('x', () => expect(x).toBe(2));\n")
        return {"summary": "s", "root_cause": "r", "tests": ["src/c.test.ts"], "changes": []}

    cross["agents"][2] = rewrites_x_only_in_a_test
    res = cross["run"]()
    assert res.ending == "fixed"
    assert res.result["candidates"][2]["ending"] == "fix-untrusted"


@pytest.mark.parametrize("kinds,ending", [
    (["not-a-defect"] * 3, "not-a-defect"),
    (["not-a-defect", "cannot-reproduce", "not-a-defect"], "no-fix"),
])
def test_every_candidate_giving_up_ends_by_their_kinds(cross, kinds, ending):
    cross["agents"] = [lambda wt, k=k: {"give_up": "no", "kind": k} for k in kinds]
    assert cross["run"]().ending == ending


def test_an_agent_outage_ends_the_run_agent_unavailable(cross):
    def outage(wt: str) -> dict:
        raise headless_agent.AgentUnavailable("You've hit your weekly limit")

    cross["agents"][1] = outage
    res = cross["run"]()
    assert res.ending == "agent-unavailable" and res.fault is True


def test_the_picked_fix_still_faces_the_host_s_checks(cross, monkeypatch):
    monkeypatch.setattr(fix_lane, "suite_proof", lambda spec, patch, label: {
        "exit": 20, "exit_confirm": 20, "confirmed": True, "flake": False, "excluded": 0,
        "new_failures": ["src/other.test.ts"]})
    res = cross["run"]()
    assert res.ending == "fix-unproven" and "other.test.ts" in res.detail


def test_every_clone_is_removed_afterward(cross):
    cross["run"]()
    assert not any(p.name.startswith("cand-") or p.name == "pick"
                   for p in cross["workdir"].iterdir() if p.is_dir())


def test_the_picked_fix_faces_the_scope_safety_reviewer_with_it_applied(cross):
    res = cross["run"]()
    [review] = cross["reviewed"]
    assert review["lens"] == "scope-safety" and "src/x.ts" in review["patch"]
    assert "reproduction" in review["evidence"]
    assert res.result["reviews"] == [cross["review"]]


def test_the_inventory_vetoes_only_on_tier_0_paths(cross, monkeypatch):
    cross["run"]()
    assert cross["reviewed"][-1]["inventory_veto"] is False
    from pipeline import risktier
    monkeypatch.setattr(risktier, "tier_facet", lambda paths: {"tier": 0, "pinned_by": paths})
    cross["run"]()
    assert cross["reviewed"][-1]["inventory_veto"] is True


def test_a_fix_the_reviewer_judges_unsafe_ends_fix_rejected(cross):
    cross["review"] = {"lens": "scope-safety", "verdict": "unsafe",
                       "reason": "widens access for viewers", "concerns": []}
    res = cross["run"]()
    assert res.ending == "fix-rejected" and "widens access" in res.detail


def test_a_reviewer_that_never_reached_a_verdict_is_a_machine_fault(cross):
    cross["review"] = {"lens": "scope-safety", "verdict": "unsafe", "failed": True,
                       "reason": "the reviewing agent did not finish", "concerns": []}
    res = cross["run"]()
    assert res.ending == "run-failed" and res.fault is True


def test_no_review_runs_when_the_host_s_checks_refuse(cross, monkeypatch):
    monkeypatch.setattr(fix_lane, "suite_proof", lambda spec, patch, label: {
        "exit": 20, "exit_confirm": 20, "confirmed": True, "flake": False, "excluded": 0,
        "new_failures": ["src/other.test.ts"]})
    cross["run"]()
    assert cross["reviewed"] == []
