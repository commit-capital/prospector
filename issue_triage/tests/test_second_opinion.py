"""Another author's pull request on an issue the factory fixed: which ones are
evidence, how their tests judge our fix, and what a gap does."""
from __future__ import annotations

from pathlib import Path

import pytest

from issue_triage import second_opinion
from pipeline import diff_cache, gates, prove


def _diff(path: str, line: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+{line}\n"


TESTS = _diff("src/x.test.ts", "test('empty', () => expect(parse('')).toBe(null));")
FIX = _diff("src/x.ts", "export const parse = (s) => s || null;")
OURS = _diff("src/y.test.ts", "test('y', () => {});") + _diff("src/x.ts", "export const x = 2;")


def _pr(number: int, **over) -> dict:
    return {"number": number, "title": f"fix {number}", "state": "open", "author": "contrib",
            "closes": True, **over}


@pytest.fixture
def found(monkeypatch):
    state: dict = {"prs": [], "diffs": {}}
    monkeypatch.setattr(second_opinion.related_prs, "search",
                        lambda issue, exclude=None: [p for p in state["prs"]
                                                     if p["number"] not in (exclude or set())])
    monkeypatch.setattr(diff_cache, "fetch_complete", lambda n: None if n not in state["diffs"]
                        else diff_cache.CompleteDiff(state["diffs"][n], ()))
    return state


def test_only_open_rivals_that_claim_the_fix_and_carry_tests_are_evidence(found, monkeypatch):
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "triagebot[bot]")
    found["prs"] = [_pr(1), _pr(2, state="closed"), _pr(3, closes=False),
                    _pr(4, author="triagebot"), _pr(5), _pr(6), _pr(7, author="blocked"),
                    _pr(8), _pr(9), _pr(10)]
    found["diffs"] = {1: TESTS + FIX, 5: FIX, 6: TESTS + FIX + "MALICIOUS", 7: TESTS + FIX,
                      8: TESTS + FIX, 9: TESTS + FIX}
    monkeypatch.setattr(second_opinion.threats, "scan_diff", lambda text: {
        "verdict": "malicious" if "MALICIOUS" in text else "clear"})
    rivals, skipped = second_opinion.rivals(70, exclude={99},
                                            registry={"actors": {"blocked": {}}})
    assert [r.pr for r in rivals] == [1, 8, 9]
    assert rivals[0].tests == TESTS and rivals[0].fix == FIX
    assert rivals[0].test_paths == ["src/x.test.ts"]
    why = {e["pr"]: e["why"] for e in skipped}
    assert set(why) == {5, 6, 7}
    assert "no tests" in why[5] and "malicious" in why[6] and "blocklist" in why[7]
    assert all(e["verdict"] == "skipped" for e in skipped)


def _legs(exit_: int) -> prove.Legs:
    return {"exit": exit_, "exit_confirm": exit_, "output_tail": f"exit {exit_}: x.test.ts > empty",
            "duration_s": 1.0}


RED, GREEN = _legs(gates.SENTINEL_TEST_FAIL), _legs(gates.SENTINEL_PASS)
RIVAL = second_opinion.Rival(pr=12, author="contrib", title="fix x", tests=TESTS, fix=FIX,
                             test_paths=["src/x.test.ts"])
BASE = prove.PinnedBase(sha="a" * 40, tier=2, image="img", clone=Path("/nonexistent"))


@pytest.fixture
def legs(monkeypatch):
    runs: dict = {"red": RED, "theirs": GREEN, "ours": GREEN, "seen": []}

    def red_legs(base, *, patch, test_cmd, label):
        runs["seen"].append(("red", patch.read_text()))
        return runs["red"]

    def green_legs(base, *, patch, test_cmd, label):
        text = patch.read_text()
        runs["seen"].append(("green", text))
        return runs["theirs"] if FIX in text else runs["ours"]

    monkeypatch.setattr(prove, "red_legs", red_legs)
    monkeypatch.setattr(prove, "green_legs", green_legs)
    return runs


@pytest.mark.parametrize("red,theirs,ours,verdict", [
    (GREEN, GREEN, GREEN, "skipped"),
    (RED, RED, GREEN, "skipped"),
    (RED, GREEN, GREEN, "covered"),
    (RED, GREEN, RED, "gap"),
])
def test_a_rival_s_tests_judge_our_fix_only_when_they_prove_its_own(legs, red, theirs, ours,
                                                                    verdict):
    legs.update(red=red, theirs=theirs, ours=ours)
    entry = second_opinion.judge(BASE, RIVAL, OURS, label="so-7")
    assert (entry["pr"], entry["author"], entry["verdict"]) == (12, "contrib", verdict)
    if verdict == "gap":
        assert "x.test.ts > empty" in entry["output"]


def test_tests_that_touch_a_file_our_fix_changes_are_skipped(legs):
    clash = second_opinion.Rival(pr=12, author="c", title="t", tests=_diff("src/y.test.ts", "t"),
                                 fix=FIX, test_paths=["src/y.test.ts"])
    entry = second_opinion.judge(BASE, clash, OURS, label="so-7")
    assert entry["verdict"] == "skipped" and "src/y.test.ts" in entry["why"]


def test_the_revision_is_told_the_gap_and_shown_the_test_never_the_change(legs):
    legs.update(ours=RED)
    entry = second_opinion.judge(BASE, RIVAL, OURS, label="so-7")
    notes = second_opinion.notes([(RIVAL, entry)])
    assert "#12" in notes and "expect(parse(''))" in notes and "x.test.ts > empty" in notes
    assert "s || null" not in notes


def test_a_recheck_closes_or_keeps_the_gap(legs):
    legs.update(ours=RED)
    entry = second_opinion.judge(BASE, RIVAL, OURS, label="so-7")
    legs.update(ours=GREEN)
    assert second_opinion.recheck(BASE, RIVAL, OURS, entry, label="so-7")["verdict"] == (
        "gap-closed")
    legs.update(ours=RED)
    assert second_opinion.recheck(BASE, RIVAL, OURS, entry, label="so-7")["verdict"] == (
        "gap-open")


def test_only_a_closed_gap_earns_credit_and_an_open_one_holds_the_fix():
    entries = [{"pr": 1, "author": "a", "verdict": "covered", "why": "w"},
               {"pr": 2, "author": "b", "verdict": "gap-closed", "why": "w"},
               {"pr": 3, "author": "c", "verdict": "skipped", "why": "w"}]
    assert second_opinion.credit(entries) == [{"pr": 2, "author": "b"}]
    assert second_opinion.flag(entries) is None
    entries.append({"pr": 4, "author": "d", "verdict": "gap-open", "why": "x.test.ts fails"})
    assert "#4" in (second_opinion.flag(entries) or "")
    assert second_opinion.flag(None) is None
