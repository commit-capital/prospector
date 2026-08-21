"""The configurable review-provider policy (#522): settings selectors, the
greptile/none profiles, and the provider-neutral Pr accessors."""
import pytest

from pipeline import review_policy, settings
from pipeline.model import Pr


# --- settings selectors ---------------------------------------------------


def test_parse_review_provider_default():
    assert settings.parse_review_provider(None) == "none"
    assert settings.parse_review_provider("") == "none"


def test_parse_review_provider_greptile():
    assert settings.parse_review_provider("Greptile") == "greptile"


def test_parse_review_provider_unknown_exits():
    with pytest.raises(SystemExit):
        settings.parse_review_provider("bogus")


# --- profiles -------------------------------------------------------------


def test_greptile_profile(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)
    p = review_policy.active()
    assert p.provider == "greptile"
    assert p.required is True
    assert p.threshold == 5
    assert p.score_max == 5
    assert p.section == "greptile_review"
    assert p.retrigger_mention == "@greptileai"


def test_none_profile(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)
    p = review_policy.active()
    assert p.provider == "none"
    assert p.required is False
    assert p.threshold is None
    assert p.section is None
    assert p.retrigger_mention is None


def test_threshold_override(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.setenv("TRIAGE_REVIEW_THRESHOLD", str(4))
    assert review_policy.active().threshold == 4


def test_threshold_override_ignored_for_none(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    monkeypatch.setenv("TRIAGE_REVIEW_THRESHOLD", str(4))
    assert review_policy.active().threshold is None


# --- clean_blocker --------------------------------------------------------


def test_clean_blocker_greptile_below_bar(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)
    pr = Pr(None, {"signals": {"greptile": 3}})
    assert review_policy.active().clean_blocker(pr) == "greptile 3/5"


def test_clean_blocker_greptile_at_bar(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)
    pr = Pr(None, {"signals": {"greptile": 5}})
    assert review_policy.active().clean_blocker(pr) is None


def test_clean_blocker_none_never_blocks(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    pr = Pr(None, {"signals": {}})
    assert review_policy.active().clean_blocker(pr) is None


# --- provider-neutral Pr accessors ---------------------------------------


def test_review_score_greptile(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    pr = Pr(None, {"signals": {"greptile": 5}})
    assert pr.review_score == 5
    assert pr.review_section == "greptile_review"


def test_review_accessors_none(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    pr = Pr(None, {"signals": {"greptile": 5}})
    assert pr.review_score is None
    assert pr.review_section is None
    assert pr.review_stale is None
    assert pr.review_severity is None
    assert pr.review_reviewed_sha is None


# --- ANALYZE merge-bar sentence ------------------------------------------


def test_merge_bar_sentence_greptile(monkeypatch):
    from pipeline import analyze_driver
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)
    text = analyze_driver.merge_bar_sentence()
    assert "Greptile" in text and "5/5" in text


def test_merge_bar_sentence_none(monkeypatch):
    from pipeline import analyze_driver
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    text = analyze_driver.merge_bar_sentence()
    assert "greptile" not in text.lower()
    assert "CI passing" in text
