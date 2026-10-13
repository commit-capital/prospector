"""design_review: the Prospector-run reviewer's agent, its answer validation
and its hunt pool."""
from __future__ import annotations

from pipeline import settings


def test_mode_defaults_to_advisory(monkeypatch):
    monkeypatch.delenv("TRIAGE_DESIGN_REVIEW", raising=False)
    assert settings.design_review_mode() == "advisory"


def test_mode_reads_choices_and_falls_back(monkeypatch):
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "Gate")
    assert settings.design_review_mode() == "gate"
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "bogus")
    assert settings.design_review_mode() == "advisory"
