"""A maintainer-approved feature request runs through the fix lane: its body
carries the approved spec, and a give-up that calls it no defect reads no-fix."""
from __future__ import annotations

from pathlib import Path

import pytest

from issue_triage import (config, cross_lane, feature_screen, fix_lane, fix_review_runner,
                          reproduce_issue, solo_lane)
from issue_triage.issue_store import IssueStore
from pipeline import prove, verify_driver

BASE = prove.PinnedBase(sha="a" * 40, tier=2, image="img", clone=Path("/nonexistent"))


def test_approved_report_keeps_issue_text_and_cuts_mockup_first():
    body = "x" * 3000
    out = feature_screen.approved_report(body, {"why": "fits", "mockup": "<html>" + "y" * 20000},
                                         None, repo="paperclip")
    assert out.startswith(body) and "Approved as a feature by a paperclip maintainer" in out
    assert "fits" in out and "<html>yyy" in out
    assert len(out) <= reproduce_issue.REPORT_MAX


def test_approved_report_gives_why_and_guidance_before_the_mockup():
    out = feature_screen.approved_report(
        "add x", {"why": "it fits", "mockup": "<html>page</html>"},
        {"decision": "implement", "by": "m", "at": "t", "guidance": "put it in the toolbar"},
        repo="paperclip")
    assert out.index("it fits") < out.index("put it in the toolbar") < out.index("<html>page")


def test_approved_report_keeps_the_approval_when_the_issue_fills_the_report():
    out = feature_screen.approved_report("x" * 20000, {"why": "fits", "mockup": "<html>"},
                                         None, repo="paperclip")
    assert "Approved as a feature by a paperclip maintainer" in out and "fits" in out
    assert "<html>" not in out and len(out) <= reproduce_issue.REPORT_MAX


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repo_name", lambda: "paperclip")
    s = IssueStore(tmp_path)
    s.save_issue({"issue": 7, "meta": {"title": "add x", "state": "open", "body": "please add x",
                                       "updated_at": "2026-10-01T00:00:00Z"}})
    s.edit_issue(7).record_feature_screen({"verdict": "consider", "why": "x fits"})
    return s


def test_feature_spec_built_from_request(store):
    spec, report = fix_review_runner._spec(store, 7, BASE, None, feature=True)
    assert spec.feature is True
    assert spec.body.startswith("please add x") and "Approved as a feature" in spec.body
    assert "x fits" in spec.body
    assert report == fix_lane.report_sha("add x", "please add x")


def test_an_implement_decision_keeps_a_follow_up_in_feature_mode(store):
    store.edit_issue(7).record_feature_decision(
        {"decision": "implement", "by": "m", "at": "t", "guidance": "in the toolbar"})
    spec, _ = fix_review_runner._spec(store, 7, BASE, "rename it")
    assert spec.feature is True and "in the toolbar" in spec.body


@pytest.mark.parametrize("decision", [None, "decline"])
def test_a_defect_spec_carries_the_report_alone(store, decision):
    if decision:
        store.edit_issue(7).record_feature_decision({"decision": decision, "by": "m", "at": "t"})
    spec, _ = fix_review_runner._spec(store, 7, BASE, None)
    assert spec.feature is False and spec.body == "please add x"


def test_a_feature_request_reaches_solve(store, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(fix_review_runner, "solve", lambda s, n, **kw: seen.update(kw) or (
        {"ending": "no-fix", "detail": "d", "result": {}}, None))
    monkeypatch.setattr(fix_review_runner, "_write_run", lambda *a: None)
    fix_review_runner._carry_out(store, 7, "solve", {"action": "solve", "feature": True},
                                 on_step=lambda step: None)
    assert seen["feature"] is True


@pytest.fixture
def lanes(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    (base_dir / "src").mkdir(parents=True)
    (base_dir / "src" / "x.ts").write_text("export const x = 1;\n")
    base = prove.PinnedBase(sha="a" * 40, tier=2, image="img", clone=base_dir)
    monkeypatch.setattr(verify_driver, "SCRATCH", tmp_path / "vscratch")
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "vscratch"))
    monkeypatch.delenv("TRIAGE_PROFILE", raising=False)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_MODELS", "opus,sonnet,opus")
    monkeypatch.setattr(solo_lane, "author",
                        lambda wt, **kw: {"give_up": "works as built", "kind": "not-a-defect"})

    def run(lane, feature: bool) -> fix_lane.LaneResult:
        spec = fix_lane.LaneSpec(issue=7, title="add x", body="please", base=base,
                                 feature=feature)
        return lane.run(spec, workdir=tmp_path / "work")

    return run


@pytest.mark.parametrize("lane", [cross_lane, solo_lane])
def test_feature_run_not_a_defect_reads_no_fix(lanes, lane):
    assert lanes(lane, True).ending == "no-fix"


@pytest.mark.parametrize("lane", [cross_lane, solo_lane])
def test_defect_run_still_ends_not_a_defect(lanes, lane):
    assert lanes(lane, False).ending == "not-a-defect"
