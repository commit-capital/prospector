"""Putting a reviewer's "not addressed" claim to a test: only a test the host
sees fail twice with the fix applied, and the judge rates the defect, shows
it. The agent, the sandbox and the judge are mocked; the clone is real."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_triage import claim_test, fix_lane, judge_repro
from pipeline import gates, prove, verify_driver

FIX = ("diff --git a/src/x.ts b/src/x.ts\n--- a/src/x.ts\n+++ b/src/x.ts\n@@ -1 +1 @@\n"
       "-export const x = 1;\n+export const x = 2;\n")
DEFECT = {"defect": "cancel in submitting is refused", "status": "not-addressed",
          "evidence": "the cancellable states exclude submitting"}
RATED = {"symptom_match": {"matches": True, "confidence": "high", "reasoning": "r"},
         "defect": {"is_defect": True, "confidence": "high", "reasoning": "r"}}


def _legs(exit_: int, confirm: int | None) -> dict:
    return {"exit": exit_, "exit_confirm": confirm, "output_tail": "expected 200", "duration_s": 1.0}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    (base_dir / "src").mkdir(parents=True)
    (base_dir / "src" / "x.ts").write_text("export const x = 1;\n")
    scratch = tmp_path / "vscratch"
    monkeypatch.setattr(verify_driver, "SCRATCH", scratch)
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(scratch))
    monkeypatch.delenv("TRIAGE_PROFILE", raising=False)
    state: dict = {"red": _legs(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL),
                   "rating": RATED, "seen": {}}

    def writes(worktree, **kw):
        state["seen"].update(x=(Path(worktree) / "src" / "x.ts").read_text(), **kw)
        (Path(worktree) / "src" / "cancel.test.ts").write_text(
            "test('cancel', () => expect(cancel()).toBe(200));\n")
        return {"files": ["src/cancel.test.ts"], "claimed_symptom": "cancel refused",
                "expected_red_signature": "expected 200"}

    state["author"] = writes
    monkeypatch.setattr(claim_test, "author", lambda wt, **kw: state["author"](wt, **kw))

    def red_legs(base_, *, patch, test_cmd, label, tail_bytes=0):
        state["red_patch"] = patch.read_text()
        return state["red"]

    monkeypatch.setattr(prove, "red_legs", red_legs)
    monkeypatch.setattr(judge_repro, "judge", lambda wt, **kw: state["rating"])
    spec = fix_lane.LaneSpec(issue=14777, title="cancel is a no-op", body="b",
                             base=prove.PinnedBase(sha="a" * 40, tier=2, image="img",
                                                   clone=base_dir))

    def run() -> claim_test.Claim:
        return claim_test.prove_claim(
            spec, defect=DEFECT, tests="", fix=FIX, workdir=tmp_path / "work",
            proof_patch=lambda *parts: prove.compose("claim-14777", *parts),
            label="claim-14777")

    state["run"] = run
    state["work"] = tmp_path / "work"
    return state


def test_a_test_that_fails_twice_with_the_fix_and_shows_the_defect_proves_the_claim(lane):
    claim = lane["run"]()
    assert claim["status"] == "shown" and claim["paths"] == ["src/cancel.test.ts"]
    assert "+++ b/src/cancel.test.ts" in claim["patch"] and "src/x.ts" not in claim["patch"]
    assert "+export const x = 2;" in lane["red_patch"]  # run with the fix applied
    assert lane["seen"]["x"] == "export const x = 2;\n"  # the agent's clone holds the fix
    assert lane["seen"]["defect"] == DEFECT["defect"]
    assert claim["agent_runs"] == 2 and claim["fix"] == claim_test.fix_digest(FIX)
    assert not (lane["work"] / "claim").exists()


@pytest.mark.parametrize("change,why", [
    ({"author": lambda wt, **kw: {"give_up": "cancelDurable handles it"}},
     "no failing test could be written: cancelDurable handles it"),
    ({"red": _legs(gates.SENTINEL_PASS, None)}, "passes with the fix applied"),
    ({"rating": {**RATED, "defect": {"is_defect": False, "confidence": "high",
                                     "reasoning": "deliberate"}}}, "intended"),
    ({"rating": {**RATED, "symptom_match": {"matches": False, "confidence": "high",
                                            "reasoning": "typo"}}}, "does not show"),
])
def test_anything_short_of_a_judged_red_leaves_the_claim_unshown(lane, change, why):
    lane.update(change)
    claim = lane["run"]()
    assert claim["status"] == "unshown" and why in claim["why"]


def test_a_claim_test_that_edits_existing_code_is_refused(lane):
    def edits(worktree, **kw):
        (Path(worktree) / "src" / "x.ts").write_text("export const x = 3;\n")
        return {"files": [], "claimed_symptom": "", "expected_red_signature": ""}

    lane["author"] = edits
    claim = lane["run"]()
    assert claim["status"] == "unshown" and "edited existing files" in claim["why"]


def test_the_claim_reaches_the_agent_as_bounded_data_on_a_locked_down_run(monkeypatch):
    seen: dict = {}

    def run_agent(prompt, **kw):
        seen.update(prompt=prompt, **kw)
        return '```json\n{"give_up": "handled at cancel.ts:12"}\n```'

    monkeypatch.setattr(claim_test.headless_agent, "run_agent", run_agent)
    out = claim_test.author("/wt", title="t", body="b", defect="cancel " + "x" * 5000,
                            evidence="see `cancel.ts`", env={}, contributor_docs=[])
    assert out == {"give_up": "handled at cancel.ts:12", "ruling": None}
    assert '{"still_present": "cancel xxx' in seen["prompt"]
    assert "x" * (claim_test.CLAIM_MAX + 10) not in seen["prompt"]
    assert seen["allow_gh"] is False and seen["edit_root"] == seen["cwd"]
    assert seen["read_root"] == [seen["cwd"]]


@pytest.mark.parametrize("ruling,want", [("outside-report", "outside-report"),
                                         ("deliberate", "deliberate"), ("bogus", None)])
def test_a_give_up_names_its_ruling_or_none(monkeypatch, ruling, want):
    answer = json.dumps({"give_up": "the report calls it optional", "ruling": ruling})
    monkeypatch.setattr(claim_test.headless_agent, "run_agent",
                        lambda prompt, **kw: f"```json\n{answer}\n```")
    out = claim_test.author("/wt", title="t", body="b", defect="d", evidence="e", env={},
                            contributor_docs=[])
    assert out == {"give_up": "the report calls it optional", "ruling": want}


@pytest.mark.parametrize("change,ruling", [
    ({"author": lambda wt, **kw: {"give_up": "the report leaves it to #2922",
                                  "ruling": "outside-report"}}, "outside-report"),
    ({"rating": {**RATED, "defect": {"is_defect": False, "confidence": "high",
                                     "reasoning": "deliberate"}}}, "deliberate"),
    ({"red": _legs(gates.SENTINEL_PASS, None)}, None),
])
def test_an_unshown_claim_carries_the_ruling_it_was_given_up_on(lane, change, ruling):
    lane.update(change)
    claim = lane["run"]()
    assert claim["status"] == "unshown" and claim["ruling"] == ruling


@pytest.mark.parametrize("setting,want", [("codex:gpt-x", "codex:gpt-x"), ("", None)])
def test_the_claim_agent_runs_on_the_review_model(monkeypatch, setting, want):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_REVIEW_MODELS", setting)
    seen: dict = {}

    def run_agent(prompt, **kw):
        seen.update(kw)
        return '```json\n{"give_up": "handled"}\n```'

    monkeypatch.setattr(claim_test.headless_agent, "run_agent", run_agent)
    claim_test.author("/wt", title="t", body="b", defect="d", evidence="e", env={},
                      contributor_docs=[])
    assert seen["model"] == want
