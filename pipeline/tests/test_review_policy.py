"""review_policy: which reviewers gate, detected or configured."""
import pytest

from pipeline import review_policy, reviewers, settings
from pipeline.model import Pr

HEAD = "h" * 40


def test_parse_modes():
    assert settings.parse_review_provider(None) == ("auto", ())
    assert settings.parse_review_provider("") == ("auto", ())
    assert settings.parse_review_provider("none") == ("none", ())
    assert settings.parse_review_provider("Greptile") == ("explicit", ("greptile",))
    assert settings.parse_review_provider("greptile, coderabbit,socket") == (
        "explicit", ("greptile", "coderabbit", "socket"))
    with pytest.raises(SystemExit):
        settings.parse_review_provider("bogus")


def test_explicit_mode_ignores_detection(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.setattr(review_policy, "_load_seen",
                        lambda: {"coderabbit": {"last_observed_at": "2999-01-01T00:00:00Z", "prs": 1}})
    review_policy.reset()
    assert [r.id for r in review_policy.active_reviewers()] == ["greptile"]
    assert review_policy.active_reviewers(reviewers.SCANNER) == []


def test_none_mode(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    review_policy.reset()
    assert review_policy.active_reviewers() == []
    assert review_policy.clean_blockers(Pr(None, {"meta": {"head_sha": HEAD}}), reviewers.REVIEW) == []


def test_auto_mode_uses_activity_window(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "auto")
    monkeypatch.setenv("TRIAGE_REVIEWER_ACTIVE_DAYS", "14")
    monkeypatch.setattr(review_policy, "_load_seen", lambda: {
        "greptile": {"last_observed_at": "2026-08-20T00:00:00Z", "prs": 10},
        "coderabbit": {"last_observed_at": "2026-06-18T00:00:00Z", "prs": 184},
        "superagent": {"last_observed_at": "2026-08-21T00:00:00Z", "prs": 3}})
    monkeypatch.setattr(review_policy, "_today", lambda: "2026-08-21")
    review_policy.reset()
    assert [r.id for r in review_policy.active_reviewers()] == ["greptile", "superagent"]
    assert review_policy.is_active("coderabbit") is False


def test_auto_mode_survives_store_failure(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "auto")

    def boom():
        raise RuntimeError("no store")
    monkeypatch.setattr(review_policy, "_load_seen", boom)
    review_policy.reset()
    assert review_policy.active_reviewers() == []


def test_clean_blockers_by_kind(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile,coderabbit,superagent")
    review_policy.reset()
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


def test_inactive_reviewer_bar_is_na(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    review_policy.reset()
    pr = Pr(None, {"meta": {"head_sha": HEAD},
                   "reviews": {"superagent": {"kind": "scanner", "findings": [
                       {"severity": "P1", "resolved": False, "outdated": False}], "checks": []}}})
    assert review_policy.bar(pr, reviewers.SUPERAGENT).status == "na"


def test_threshold_override_applies_to_greptile(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.setenv("TRIAGE_REVIEW_THRESHOLD", "4")
    review_policy.reset()
    pr = Pr(None, {"meta": {"head_sha": HEAD}, "reviews": {"greptile": {
        "kind": "review", "score": 4, "reviewed_sha": HEAD, "findings": [], "checks": []}}})
    assert review_policy.bar(pr, reviewers.GREPTILE).status == "pass"
    assert review_policy.greptile_threshold() == 4


def test_merge_bar_sentence(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile,superagent")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)
    review_policy.reset()
    s = review_policy.merge_bar_sentence()
    assert "Greptile at 5/5" in s and "Superagent" in s and "CI passing" in s
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    review_policy.reset()
    assert review_policy.merge_bar_sentence() == "CI passing, mergeable (no conflicts)"


def test_describe(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    review_policy.reset()
    d = {x["id"]: x for x in review_policy.describe()}
    assert d["greptile"]["active"] is True and d["greptile"]["retrigger"] is True
    assert d["greptile"]["threshold"] == 5
    assert d["socket"]["active"] is False and d["socket"]["kind"] == "scanner"
    assert d["coderabbit"]["threshold"] is None
