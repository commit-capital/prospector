"""The cross-tested lane: independent candidates reproduce and fix; every fix
runs against every reproduction, and only an agreed fix is judged further. The
agents and the sandbox are mocked — the sandbox passes a pairing of a test and
a fix by a table — while the clones, the re-gate and the patch splitting run
for real."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from issue_triage import cross_lane, fix_lane, review_issue_fix, solo_lane
from pipeline import authoring, gates, headless_agent, prove, resolve_evidence, verify_driver


def _legs(exit_: int, confirm: int | None) -> dict:
    return {"exit": exit_, "exit_confirm": confirm, "output_tail": "", "duration_s": 1.0}


RED = _legs(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL)
GREEN = _legs(gates.SENTINEL_PASS, gates.SENTINEL_PASS)
FAILS = _legs(gates.SENTINEL_TEST_FAIL, None)


def _writes(test: str, value: int, extra_lines: int = 0, *, sets: int | None = None):
    """An agent that writes the test `test` (expecting x == `value`) and sets x
    to `sets`, which defaults to `value`."""
    def author(wt: str) -> dict:
        (Path(wt) / "src" / test).write_text(f"test('x', () => expect(x).toBe({value}));\n")
        body = (f"export const x = {value if sets is None else sets};\n"
                + "// pad\n" * extra_lines)
        (Path(wt) / "src" / "x.ts").write_text(body)
        return {"summary": "Fix x", "root_cause": "x", "tests": [f"src/{test}"],
                "changes": [{"path": "src/x.ts", "rationale": "set x"}]}
    return author


def _extends(title: str, value: int):
    """An agent that adds the case `title` (expecting x == `value`) to the
    existing src/x.test.ts and sets x."""
    def author(wt: str) -> dict:
        with (Path(wt) / "src" / "x.test.ts").open("a") as f:
            f.write(f"test('{title}', () => expect(x).toBe({value}));\n")
        (Path(wt) / "src" / "x.ts").write_text(f"export const x = {value};\n")
        return {"summary": "Fix x", "root_cause": "x", "tests": ["src/x.test.ts"],
                "changes": [{"path": "src/x.ts", "rationale": "set x"}]}
    return author


def _report(failing: list[str]) -> str:
    """A test runner's output tail whose end-of-run report names `failing`."""
    if not failing:
        return " Test Files  1 passed (1)\n"
    return f" Failed Tests {len(failing)} \n\n" + "".join(f" FAIL  {t}\n" for t in failing)


_CASE_RE = re.compile(r"^[ +]test\('([^']+)', \(\) => expect\(x\)\.toBe\((\d+)\)\);$")


def _failing(patch_text: str) -> list[str]:
    """The cases a patch's test files fail with the x its fix sets (1 without
    one), reading every case the patch shows, context lines included."""
    x = 1
    for line in patch_text.splitlines():
        if line.startswith("+export const x = "):
            x = int(line.split("= ")[1].rstrip(";"))
    failing: list[str] = []
    path = ""
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):]
        m = _CASE_RE.match(line)
        if m and int(m.group(2)) != x:
            failing.append(f"{path} > {m.group(1)}")
    return failing


@pytest.fixture
def runner(cross, monkeypatch):
    """The cross lane over a base whose src/x.test.ts already pins x at 1, with
    a sandbox that runs every case a patch shows and reports the ones that
    fail."""
    (cross["base_dir"] / "src" / "x.test.ts").write_text(
        "test('keeps x at 1', () => expect(x).toBe(1));\n")

    def fake_red(base_, *, patch, test_cmd, label, tail_bytes=0):
        failing = _failing(patch.read_text())
        if not failing:
            return _legs(gates.SENTINEL_PASS, None)
        return {**_legs(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL),
                "output_tail": _report(failing)}

    def fake_green(base_, *, patch, test_cmd, label, tail_bytes=0):
        failing = _failing(patch.read_text())
        if not failing:
            return GREEN
        return {**FAILS, "output_tail": _report(failing)}

    monkeypatch.setattr(prove, "red_legs", fake_red)
    monkeypatch.setattr(prove, "green_legs", fake_green)
    return cross


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
                   "models": [], "docs": [], "green_runs": 0}

    def fake_author(worktree, *, title, body, env, model=None, guidance=None, attempt=None,
                    contributor_docs=(), on_event=None):
        index = int(Path(worktree).parent.name.split("-")[1])
        state["models"].append((index, model))
        state["docs"].append(list(contributor_docs))
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
        assert (Path(worktree) / "src" / "x.ts").read_text() != "export const x = 1;\n"
        return state["review"]

    monkeypatch.setattr(review_issue_fix, "review", fake_review)

    def run() -> fix_lane.LaneResult:
        spec = fix_lane.LaneSpec(issue=7, title="x is wrong", body="x should be 2", base=base)
        return cross_lane.run(spec, workdir=tmp_path / "work")

    state["run"] = run
    state["workdir"] = tmp_path / "work"
    state["base_dir"] = base_dir
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


def test_each_candidate_is_handed_the_base_s_contributor_docs(cross):
    (cross["base_dir"] / "AGENTS.md").write_text("Reuse the helpers in src/util.\n")
    cross["run"]()
    assert cross["docs"] == [[authoring.Doc("AGENTS.md", "Reuse the helpers in src/util.")]] * 3


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


def test_a_dispute_records_each_reading_the_candidates_split_into(cross):
    cross["agents"] = [_writes("a.test.ts", 2), _writes("b.test.ts", 3),
                       _writes("c.test.ts", 2, extra_lines=1)]
    res = cross["run"]()
    assert res.ending == "fix-disputed"
    assert res.result["readings"] == [[0, 2], [1]]
    kept = {c["index"]: c for c in res.result["candidate_patches"]}
    assert kept[1]["verdict"]["summary"] == "Fix x" and kept[1]["test_paths"] == ["src/b.test.ts"]


@pytest.mark.parametrize("reading,shipped", [([0, 2], [0, 2]), ([1], [1])])
def test_an_answer_resumes_a_dispute_on_the_reading_it_chose(cross, reading, shipped):
    cross["agents"] = [_writes("a.test.ts", 2), _writes("b.test.ts", 3),
                       _writes("c.test.ts", 2, extra_lines=1)]
    disputed = cross["run"]()
    before = cross["green_runs"]
    spec = fix_lane.LaneSpec(issue=7, title="x is wrong", body="x should be 2",
                             base=prove.PinnedBase(sha="a" * 40, tier=2, image="img",
                                                   clone=cross["workdir"].parent / "base"))
    res = cross_lane.judge_reading(spec, workdir=cross["workdir"], result=disputed.result,
                                   reading=reading)
    assert res.ending == "fixed", res.detail
    assert res.result["pick"] == reading[0] and res.result["reading"] == reading
    assert res.result["agreement"]["shipped"] == shipped
    assert res.agent_runs == 1
    assert cross["green_runs"] == before  # the cross-test results are read, not re-run


def test_a_reading_with_no_fix_passing_its_reproductions_is_unproven(cross):
    cross["agents"] = [_writes("a.test.ts", 2), _writes("b.test.ts", 3),
                       _writes("c.test.ts", 4)]
    disputed = cross["run"]()
    spec = fix_lane.LaneSpec(issue=7, title="t", body="b",
                             base=prove.PinnedBase(sha="a" * 40, tier=2, image="img",
                                                   clone=cross["workdir"].parent / "base"))
    res = cross_lane.judge_reading(spec, workdir=cross["workdir"], result=disputed.result,
                                   reading=[0, 1])
    assert res.ending == "fix-unproven"


# --- a reading is a fix that passes its own reproduction ---------------------------

def _cand(index: int, passes: dict[int, bool], reproduces: bool = True) -> cross_lane.Candidate:
    return cross_lane.Candidate(index=index, model="opus", reproduces=reproduces, passes=passes)


def test_a_fix_that_fails_its_own_reproduction_forms_no_reading():
    # Issue 14878: candidates 0 and 2 each reproduce, and no fix passes either test.
    live = [_cand(0, {0: False, 2: False}), _cand(2, {0: False, 2: False})]
    assert cross_lane.readings(live) == []


def test_a_fix_that_fails_its_own_reproduction_joins_no_reading():
    live = [_cand(0, {0: True, 1: True}), _cand(1, {0: True, 1: False}),
            _cand(2, {0: True, 1: True}, reproduces=False)]
    assert [[c.index for c in g] for g in cross_lane.readings(live)] == [[0, 2]]


def test_no_fix_passing_its_own_reproduction_is_unproven_not_disputed(cross):
    cross["agents"] = [_writes("a.test.ts", 2, sets=3), _writes("b.test.ts", 2, sets=4),
                       _writes("c.test.ts", 2, sets=5)]
    res = cross["run"]()
    assert res.ending == "fix-unproven" and res.fault is False
    assert "own reproduction" in res.detail
    assert "readings" not in res.result


def test_one_reading_is_no_dispute(cross):
    cross["agents"] = [_writes("a.test.ts", 2), _writes("b.test.ts", 3, sets=4),
                       _writes("c.test.ts", 5, sets=6)]
    res = cross["run"]()
    assert res.ending == "fix-unproven"
    assert "one reading" in res.detail
    assert "readings" not in res.result


# --- an existing test the fixes all fail ------------------------------------------

PINNED = "src/x.test.ts > keeps x at 1"


def test_fixes_that_agree_but_for_an_existing_test_end_fix_pinned_naming_it(runner):
    runner["agents"] = [_extends("sets x to 2", 2), _extends("x becomes 2", 2),
                        _extends("x reads 2", 2)]
    res = runner["run"]()
    assert res.ending == "fix-pinned" and res.fault is False
    assert PINNED in res.detail
    assert res.result["agreement"]["agreed"] == []
    assert res.result["agreement"]["pinned"] == {"agreed": [0, 1, 2], "tests": [PINNED]}
    assert res.result["candidates"][0]["pinned"] == {"0": [PINNED], "1": [PINNED],
                                                      "2": [PINNED]}
    assert "readings" not in res.result
    assert runner["reviewed"] == []


def test_fixes_that_disagree_beyond_an_existing_test_are_not_pinned(runner):
    runner["agents"] = [_extends("sets x to 2", 2), _extends("sets x to 3", 3),
                        _extends("sets x to 4", 4)]
    res = runner["run"]()
    assert res.ending == "fix-unproven"
    assert "pinned" not in res.result["agreement"] and "readings" not in res.result
    assert res.detail == ("no candidate's fix passes its own reproduction; "
                          f"existing tests the fixes fail: {PINNED}")


def _tail_legs(exit_: int, failing: list[str]) -> prove.Legs:
    return prove.Legs(exit=exit_, exit_confirm=None, output_tail=_report(failing),
                      duration_s=1.0)


REPRO_PATCH = (
    "diff --git a/src/x.test.ts b/src/x.test.ts\n"
    "--- a/src/x.test.ts\n"
    "+++ b/src/x.test.ts\n"
    "@@ -1 +1,2 @@\n"
    " test('keeps x at 1', () => expect(x).toBe(1));\n"
    "+test('sets x to 2', () => expect(x).toBe(2));\n")
BASE_TESTS = {"src/x.test.ts": "test('keeps x at 1', () => expect(x).toBe(1));\n"}
RED_LEGS = _tail_legs(gates.SENTINEL_TEST_FAIL, ["src/x.test.ts > sets x to 2"])


def test_an_existing_test_a_fix_alone_fails_is_pinned():
    green = _tail_legs(gates.SENTINEL_TEST_FAIL, [PINNED])
    assert cross_lane.pinned_failures(green, RED_LEGS, REPRO_PATCH, BASE_TESTS) == [PINNED]


@pytest.mark.parametrize("failing,base_tests", [
    # The reproduction itself still fails.
    ([PINNED, "src/x.test.ts > sets x to 2"], BASE_TESTS),
    # A case the reproduction wrote, which passed on the unfixed tree.
    (["src/x.test.ts > sets x to 2 again"], BASE_TESTS),
    # A title the unfixed file does not carry, such as an expanded test.each case.
    (["src/x.test.ts > keeps x at 1 for gpt-5"], BASE_TESTS),
    # The file is new, so no test in it is the repository's.
    ([PINNED], {}),
])
def test_a_failure_that_is_not_only_existing_tests_is_not_pinned(failing, base_tests):
    patch = REPRO_PATCH + "+test('sets x to 2 again', () => expect(x).toBe(1));\n"
    green = _tail_legs(gates.SENTINEL_TEST_FAIL, failing)
    assert cross_lane.pinned_failures(green, RED_LEGS, patch, base_tests) == []


def test_a_run_whose_report_does_not_parse_names_nothing_pinned():
    green = prove.Legs(exit=gates.SENTINEL_TEST_FAIL, exit_confirm=None,
                       output_tail="Unhandled Error\n" + _report([PINNED]), duration_s=1.0)
    assert cross_lane.pinned_failures(green, RED_LEGS, REPRO_PATCH, BASE_TESTS) == []
    unparsed_red = prove.Legs(exit=gates.SENTINEL_TEST_FAIL, exit_confirm=gates.SENTINEL_TEST_FAIL,
                              output_tail="", duration_s=1.0)
    green = _tail_legs(gates.SENTINEL_TEST_FAIL, [PINNED])
    assert cross_lane.pinned_failures(green, unparsed_red, REPRO_PATCH, BASE_TESTS) == []
