"""Showing the default branch already does what an issue asks: only a test the
host sees pass twice, which a judge reads as checking the report, shows it.
The agents and the sandbox are mocked; the clone is real."""
from __future__ import annotations

from pathlib import Path

import pytest

from issue_triage import fixed_test
from pipeline import gates, prove, verify_driver

CHECKS = {"checks_report": True, "confidence": "high", "reasoning": "r"}


def _legs(exit_: int, confirm: int | None) -> dict:
    return {"exit": exit_, "exit_confirm": confirm, "output_tail": "1 passed", "duration_s": 1.0}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    (base_dir / "src").mkdir(parents=True)
    (base_dir / "src" / "x.ts").write_text("export const x = 2;\n")
    scratch = tmp_path / "vscratch"
    monkeypatch.setattr(verify_driver, "SCRATCH", scratch)
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(scratch))
    monkeypatch.delenv("TRIAGE_PROFILE", raising=False)
    state: dict = {"green": _legs(gates.SENTINEL_PASS, gates.SENTINEL_PASS), "ruling": CHECKS}

    def writes(worktree, **kw):
        (Path(worktree) / "src" / "x.test.ts").write_text(
            "test('x', () => expect(x).toBe(2));\n")
        return {"files": ["src/x.test.ts"], "asserts": "x is 2"}

    state["author"] = writes
    monkeypatch.setattr(fixed_test, "author", lambda wt, **kw: state["author"](wt, **kw))

    def green_legs(base_, *, patch, test_cmd, label, tail_bytes=0):
        state["patch"] = patch.read_text()
        return state["green"]

    monkeypatch.setattr(prove, "green_legs", green_legs)

    def judge(wt, **kw):
        state["judged"] = kw["files"]
        return state["ruling"]

    monkeypatch.setattr(fixed_test, "judge", judge)
    base = prove.PinnedBase(sha="a" * 40, tier=2, image="img", clone=base_dir)
    state["work"] = tmp_path / "work"
    state["run"] = lambda: fixed_test.prove_fixed(14, base, title="x should be 2", body="b",
                                                  workdir=state["work"], label="close-14")
    return state


def test_a_test_that_passes_twice_and_checks_the_report_shows_it_fixed(lane):
    proof = lane["run"]()
    assert proof["status"] == "shown" and proof["paths"] == ["src/x.test.ts"]
    assert "+++ b/src/x.test.ts" in lane["patch"] and "src/x.ts\n" not in lane["patch"]
    assert lane["judged"][0]["path"] == "src/x.test.ts"
    assert not lane["work"].exists()


@pytest.mark.parametrize(("change", "why"), [
    ({"author": lambda wt, **kw: {"give_up": "x is still 1 in src/x.ts"}},
     "no passing test could be written"),
    ({"green": _legs(gates.SENTINEL_TEST_FAIL, None)}, "does not pass twice"),
    ({"green": _legs(gates.SENTINEL_PASS, gates.SENTINEL_TEST_FAIL)}, "does not pass twice"),
    ({"ruling": {**CHECKS, "checks_report": False}}, "judge does not read"),
    ({"ruling": {**CHECKS, "confidence": "medium"}}, "judge does not read"),
])
def test_anything_short_of_a_judged_pass_leaves_it_unshown(lane, change, why):
    lane.update(change)
    proof = lane["run"]()
    assert proof["status"] == "unshown" and why in proof["why"]


def test_a_test_that_edits_existing_code_is_refused(lane):
    def edits(worktree, **kw):
        (Path(worktree) / "src" / "x.ts").write_text("export const x = 3;\n")
        (Path(worktree) / "src" / "x.test.ts").write_text("test('x', () => {});\n")
        return {"files": ["src/x.test.ts"], "asserts": "x"}

    lane["author"] = edits
    proof = lane["run"]()
    assert proof["status"] == "unshown" and "edited existing files" in proof["why"]
    assert "patch" not in lane


def test_an_undisclosed_file_is_refused(lane):
    def sneaks(worktree, **kw):
        (Path(worktree) / "src" / "x.test.ts").write_text("test('x', () => {});\n")
        (Path(worktree) / "src" / "y.test.ts").write_text("test('y', () => {});\n")
        return {"files": ["src/x.test.ts"], "asserts": "x"}

    lane["author"] = sneaks
    assert lane["run"]()["status"] == "unshown" and "patch" not in lane


def test_a_test_matching_a_threat_signature_never_runs(lane, monkeypatch):
    monkeypatch.setattr("pipeline.threats.scan_diff", lambda patch: {"verdict": "blocked"})
    proof = lane["run"]()
    assert "threat signature" in proof["why"] and "patch" not in lane
