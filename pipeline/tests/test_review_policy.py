"""review_policy: which reviewers gate, detected from the repository."""
from pipeline import review_policy, reviewers
from pipeline.model import Pr

HEAD = "h" * 40


def test_no_reviewer_seen(active_reviewers):
    active_reviewers()
    assert review_policy.active_reviewers() == []
    assert review_policy.clean_blockers(Pr(None, {"meta": {"head_sha": HEAD}}), reviewers.REVIEW) == []


def test_activity_window(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEWER_ACTIVE_DAYS", "14")
    monkeypatch.setattr(review_policy, "_load_seen", lambda: {
        "greptile": {"last_observed_at": "2026-08-20T00:00:00Z", "prs": 10},
        "coderabbit": {"last_observed_at": "2026-06-18T00:00:00Z", "prs": 184},
        "superagent": {"last_observed_at": "2026-08-21T00:00:00Z", "prs": 3}})
    monkeypatch.setattr(review_policy, "_today", lambda: "2026-08-21")
    review_policy.reset()
    assert [r.id for r in review_policy.active_reviewers()] == ["greptile", "superagent"]
    assert review_policy.is_active("coderabbit") is False


def test_survives_store_failure(monkeypatch):
    def boom():
        raise RuntimeError("no store")
    monkeypatch.setattr(review_policy, "_load_seen", boom)
    review_policy.reset()
    assert review_policy.active_reviewers() == []


def test_clean_blockers_by_kind(active_reviewers):
    active_reviewers("greptile", "coderabbit", "superagent")
    pr = Pr(None, {"meta": {"head_sha": HEAD},
                   "reviews": {"greptile": {"kind": "review", "score": 5, "reviewed_sha": HEAD,
                                            "findings": [], "checks": []},
                               "superagent": {"kind": "scanner", "reviewed_sha": HEAD, "checks": [],
                                              "findings": [{"severity": "P1", "resolved": False,
                                                            "outdated": False}]}}})
    rev = review_policy.clean_blockers(pr, reviewers.REVIEW)
    assert [(b.reviewer.id, b.bar.status) for b in rev] == [("coderabbit", "pending")]
    scan = review_policy.clean_blockers(pr, reviewers.SCANNER)
    assert [(b.reviewer.id, b.bar.reason) for b in scan] == [("superagent", "superagent: 1 open P1 finding")]


def test_inactive_reviewer_bar_is_na():
    pr = Pr(None, {"meta": {"head_sha": HEAD},
                   "reviews": {"superagent": {"kind": "scanner", "findings": [
                       {"severity": "P1", "resolved": False, "outdated": False}], "checks": []}}})
    assert review_policy.bar(pr, reviewers.SUPERAGENT).status == "na"


def test_threshold_override_applies_to_greptile(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_THRESHOLD", "4")
    pr = Pr(None, {"meta": {"head_sha": HEAD}, "reviews": {"greptile": {
        "kind": "review", "score": 4, "reviewed_sha": HEAD, "findings": [], "checks": []}}})
    assert review_policy.bar(pr, reviewers.GREPTILE).status == "pass"
    assert review_policy.greptile_threshold() == 4


def test_merge_bar_sentence(active_reviewers):
    active_reviewers("greptile", "superagent")
    s = review_policy.merge_bar_sentence()
    assert "Greptile at 5/5" in s and "Superagent" in s and "CI passing" in s
    active_reviewers()
    assert review_policy.merge_bar_sentence() == "CI passing, mergeable (no conflicts)"


def test_describe():
    d = {x["id"]: x for x in review_policy.describe()}
    assert d["greptile"]["active"] is True and d["greptile"]["retrigger"] is True
    assert d["greptile"]["threshold"] == 5
    assert d["socket"]["active"] is False and d["socket"]["kind"] == "scanner"
    assert d["coderabbit"]["threshold"] is None


def _design_entry(findings: list[dict]) -> dict:
    return {"kind": "review", "reviewed_sha": HEAD, "findings": findings, "checks": [],
            "extra": {"rubric": reviewers.DESIGN_RUBRIC}}


_BLOCKING = {"severity": "blocking", "verification": {"verdict": "confirmed", "reason": "r"},
             "resolved": False, "outdated": False}


def test_never_detects_design(monkeypatch):
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "advisory")
    monkeypatch.setattr(review_policy, "_load_seen",
                        lambda: {"design": {"last_observed_at": "2999-01-01T00:00:00Z", "prs": 9}})
    review_policy.reset()
    assert "design" not in [r.id for r in review_policy.active_reviewers()]


def test_gate_mode_activates_design(monkeypatch):
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "gate")
    assert [r.id for r in review_policy.active_reviewers()] == ["greptile", "design"]


def test_advisory_design_shows_its_own_bar_without_gating(monkeypatch):
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "advisory")
    pr = Pr(None, {"meta": {"head_sha": HEAD}, "reviews": {"design": _design_entry([_BLOCKING])}})
    d = reviewers.digests(pr)["design"]
    assert (d["status"], d["advisory"]) == ("fail", True)
    assert review_policy.bar(pr, reviewers.DESIGN).status == "na"
    assert review_policy.clean_blockers(pr, reviewers.REVIEW)[0].reviewer.id == "greptile"


def test_gating_design_blocks(monkeypatch, active_reviewers):
    active_reviewers()
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "gate")
    pr = Pr(None, {"meta": {"head_sha": HEAD}, "reviews": {"design": _design_entry([_BLOCKING])}})
    assert [b.reviewer.id for b in review_policy.clean_blockers(pr, reviewers.REVIEW)] == ["design"]
    assert reviewers.digests(pr)["design"]["advisory"] is False


def test_design_never_gates_while_the_lane_is_off(monkeypatch):
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "off")
    assert [r.id for r in review_policy.active_reviewers()] == ["greptile"]
