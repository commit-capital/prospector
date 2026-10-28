from __future__ import annotations

import pytest

from issue_triage import fix_lane, fix_outcome, fix_review_runner
from issue_triage.issue_store import IssueStore
from pipeline import outcomes, prove


@pytest.mark.parametrize(("ending", "fault", "detail", "stage", "blame"), [
    ("fixed", False, "3 of 3 fixes pass", "fixed", None),
    ("sandbox", True, "the suite run did not complete", "sandbox", "machine"),
    ("agent-unavailable", True, "usage limit reached", "agent", "machine"),
    ("base-compile", True, "the type-check fails on master", "base", "machine"),
    ("not-a-defect", False, "asks for a feature", "not-a-defect", "subject"),
    ("fix-disputed", False, "the candidates read the report differently", "readings", "subject"),
    ("fix-unproven", False, "the compile lane did not pass: ui typecheck", "compile", "judged"),
    ("fix-unproven", False, "the fix makes the full suite fail beyond", "suite", "judged"),
    ("fix-unproven", False, "1 independent reproduction(s); an agreed fix needs 2",
     "reproduction", "judged"),
    ("fix-unproven", False, "only one reading of the report has a fix", "readings", "judged"),
    ("fix-rejected", False, "the revision no longer passes the reproductions", "revision",
     "judged"),
    ("fix-untrusted", False, "the fix touches a tier-0 path", "guardrail", "judged"),
    ("no-fix", False, "opus: no-fix; sonnet: no-fix", "no-fix", "judged"),
    ("fix-unproven", False, "the reproduction is not red twice on the base", "proof", "judged"),
    ("fix-unproven", False, "the agent's tests are not green twice with the fix applied",
     "proof", "judged"),
    ("fix-unproven", False, "the lint sandbox run did not pass", "compile", "judged"),
    ("fix-unproven", False, "the related-tests sandbox run did not pass: exit 1", "suite",
     "judged"),
    ("fix-unproven", False, "no candidate's fix passes its own reproduction", "no-fix",
     "judged"),
    ("cancelled", False, "issue-closed", "declined", "subject"),
    ("refused", False, "the intake audit reads the report as malicious", "declined", "subject"),
    ("declined", False, "the agent declined", "declined", "judged"),
])
def test_a_run_ending_names_its_stage_and_whose_fault(ending, fault, detail, stage, blame):
    got = fix_outcome.of_run(5, "r" * 16, ending=ending, fault=fault, detail=detail,
                             trigger="hunter")
    assert (got["stage"], got["fault"], got["words"]) == (stage, blame, detail)
    assert got["subject"] == {"kind": "issue", "number": 5, "sha": "r" * 16}


def test_a_review_rejection_names_its_reviewer():
    got = fix_outcome.of_run(5, None, ending="fix-rejected", fault=False,
                             detail="design (opus): it restates the rule a third time",
                             trigger="hunter")
    assert (got["stage"], got["by"]) == ("review", "design (opus)")


def test_a_revision_carries_who_asked_and_their_words():
    got = fix_outcome.of_run(5, None, ending="fix-untrusted", fault=False, detail="d",
                             trigger="followup",
                             extra={"notes": "Greptile: delete one test", "requested_by": "Greptile"})
    assert got["asked"] == {"by": "Greptile", "words": "Greptile: delete one test"}


def test_an_older_row_without_an_outcome_reads_from_its_ending():
    row = {"phase": "issue-fix:run", "issue": 5, "trigger": "hunter",
           "stats": {"ending": "fix-rejected", "fault": False, "report_sha": "r",
                     "detail": "scope-safety: the guard sits in the shared path"}}
    got = fix_outcome.of_row(row)
    assert got is not None and (got["stage"], got["by"]) == ("review", "scope-safety")
    assert fix_outcome.of_row({"phase": "ingest"}) is None


def test_an_outcome_rejects_a_stage_its_lane_lacks():
    with pytest.raises(ValueError):
        outcomes.make("issue-fix", {"kind": "issue", "number": 1, "sha": None}, "ci",
                      fault="judged")
    assert outcomes.read({"lane": "issue-fix", "stage": "ci",
                          "subject": {"kind": "issue", "number": 1}}) is None


def test_record_result_logs_the_outcome_and_what_a_partial_fix_leaves(tmp_path, monkeypatch):
    store = IssueStore(tmp_path / "store")
    monkeypatch.setattr(fix_outcome, "leaves", lambda result: ["the second symptom"])
    res = fix_lane.LaneResult(ending="fixed", fault=False, detail="ok", result={})
    fix_lane.record_result(store, tmp_path / "w", issue=5, report="r", base=prove.PinnedBase(
        sha="b" * 40, tier=1, image="i", clone=tmp_path), action="fix", lane="cross", models=["m"],
        res=res, started="s", finished="f", trigger="followup",
        extra=fix_review_runner._words(None, "n", "Greptile"))
    row = store.runs()[-1].raw
    assert row["stats"]["leaves"] == ["the second symptom"]
    assert row["outcome"]["stage"] == "fixed"
    assert row["outcome"]["asked"] == {"by": "Greptile", "words": "n"}


@pytest.mark.parametrize(("by", "outside", "stage", "blame"), [
    ("CI", False, "ci", "judged"),
    ("CI", True, "ci-outside", "subject"),
    ("a maintainer", False, "maintainer", "judged"),
    ("Greptile and CodeRabbit", False, "reviewer", "judged"),
    (None, False, "fixer", "judged"),
])
def test_a_hand_back_names_whose_bar_the_change_missed(by, outside, stage, blame):
    row = fix_outcome.handback_row(5, 50, "h" * 40, by=by, reason="6 revisions spent",
                                   outside=outside, revisions=6, words="their findings")
    got = outcomes.read(row["outcome"])
    assert got is not None and (got["stage"], got["fault"], got["by"]) == (stage, blame, by)
    assert got["subject"] == {"kind": "pr", "number": 50, "sha": "h" * 40}
    assert got["words"] == "6 revisions spent\n\ntheir findings"


def test_a_failed_send_back_logs_a_fixer_hand_back(tmp_path):
    store = IssueStore(tmp_path / "store")
    store.save_issue({"issue": 5, "meta": {"title": "t", "state": "open",
                                           "updated_at": "2026-10-01T00:00:00Z"}})
    fix_review_runner._hand_back(store, 5, 50, "the revision ended no-fix", head_sha="h" * 40)
    row = store.runs()[-1].raw
    assert (row["phase"], row["outcome"]["stage"]) == ("issue-fix:handback", "fixer")
