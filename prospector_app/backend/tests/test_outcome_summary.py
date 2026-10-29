from __future__ import annotations

from issue_triage import fix_outcome
from pipeline import pr_outcomes
from issue_triage.issue_model import Issue
from prospector_app.backend import issue_data, outcome_summary


def _run(issue: int, ending: str, detail: str, at: str, **stats: object) -> dict:
    return {"phase": "issue-fix:run", "issue": issue, "finished": at, "trigger": "hunter",
            "stats": {"ending": ending, "fault": ending == "sandbox", "detail": detail, **stats}}


ROWS = [
    _run(1, "fix-rejected", "design (opus): restates the rule", "2026-10-20T10:00:00+00:00"),
    _run(2, "fix-rejected", "scope-safety: hides a field", "2026-10-21T10:00:00+00:00"),
    _run(2, "fix-rejected", "scope-safety: still hides it", "2026-10-22T10:00:00+00:00"),
    _run(3, "sandbox", "the suite run did not complete", "2026-10-27T10:00:00+00:00"),
    _run(4, "fixed", "ok", "2026-10-27T11:00:00+00:00"),
    _run(5, "fixed", "ok", "2026-10-27T12:00:00+00:00", leaves=["the second symptom"]),
    _run(6, "no-fix", "too old", "2026-09-01T00:00:00+00:00"),
    fix_outcome.handback_row(7, 70, "h" * 40, by="Greptile", reason="6 revisions spent",
                             outside=False, revisions=6, words="delete one test"),
]
ROWS[-1]["finished"] = "2026-10-28T00:00:00+00:00"


def test_the_window_groups_stops_by_stage_with_their_words():
    attempts, groups, repeats, weeks = outcome_summary.summarize(
        ROWS, since="2026-10-01T00:00:00+00:00")
    assert attempts == 7
    by_stage = {(g["lane"], g["stage"]): g for g in groups}
    review = by_stage[("issue-fix", "review")]
    assert review["count"] == 3 and review["cases"][0]["words"] == "scope-safety: still hides it"
    assert [c["by"] for c in review["cases"]] == ["scope-safety", "scope-safety", "design (opus)"]
    assert by_stage[("issue-fix", "sandbox")]["fault"] == "machine"
    assert by_stage[("issue-fix", "fixed")]["cases"][0]["leaves"] == ["the second symptom"]
    assert by_stage[("issue-fix", "fixed")]["count"] == 1
    handback = by_stage[("issue-followup", "reviewer")]["cases"][0]
    assert (handback["issue"], handback["pr"], handback["by"]) == (7, 70, "Greptile")
    assert repeats == [{"lane": "issue-fix", "stage": "review", "kind": "issue", "number": 2,
                        "issue": 2, "count": 2, "last": "2026-10-22T10:00:00+00:00"}]
    assert weeks == [
        {"week": "2026-10-19", "ok": 0, "machine": 0, "subject": 0, "judged": 3},
        {"week": "2026-10-26", "ok": 2, "machine": 1, "subject": 0, "judged": 1}]


def test_pr_lane_rows_group_and_repeat_like_the_issue_lane_s():
    def fix_row(at: str) -> dict:
        return {"phase": "fix:single", "pr": 15927, "finished": at, "stats": {},
                "outcome": pr_outcomes.autofix(15927, "h" * 40, action="resolve",
                                               status="failed", detail="no diff",
                                               kind="run-failed", judged=None)}
    rows = [fix_row("2026-10-21T10:00:00+00:00"), fix_row("2026-10-22T10:00:00+00:00"),
            {"phase": "design-review:run", "pr": 9, "finished": "2026-10-22T11:00:00+00:00",
             "outcome": pr_outcomes.design(9, "h" * 40, "reviewed")}]
    attempts, groups, repeats, _ = outcome_summary.summarize(
        rows, since="2026-10-01T00:00:00+00:00")
    assert attempts == 3
    assert [(g["lane"], g["stage"], g["count"], g["cases"][0]["pr"]) for g in groups] == [
        ("autofix", "run-failed", 2, 15927)]
    assert [(r["lane"], r["kind"], r["number"], r["count"]) for r in repeats] == [
        ("autofix", "pr", 15927, 2)]


def _issue(n: int, run: dict, **sections: object) -> Issue:
    return Issue(None, {"issue": n, "meta": {"title": f"t{n}", "state": "open",
                                             "author": "boss", "author_association": "OWNER"},
                        "fix_run": run, **sections})


def test_parked_fixes_group_by_why_they_are_held(monkeypatch):
    fixed = {"ending": "fixed", "detail": "ok", "patch": "p", "boundary": {"crossings": []}}
    issues = {
        1: _issue(1, {**fixed, "agreement": {"single": True}}),
        2: _issue(2, {**fixed, "agreement": {"single": True}}),
        3: _issue(3, fixed),
        4: _issue(4, {**fixed, "proposal": {"pr": 40}},
                  fix_followup={"pr": 40, "state": "handed-back", "reason": "6 revisions spent"}),
        5: _issue(5, {"ending": "no-fix", "detail": "nothing"}),
    }
    monkeypatch.setattr(issue_data, "issues", lambda: issues)
    got = {p["kind"]: [c["issue"] for c in p["cases"]] for p in outcome_summary.parked()}
    assert got == {"single": [1, 2], "ready": [3], "handed-back": [4]}
