"""The capabilities `review` descriptor drives the frontend's provider-specific UI
(#522). It follows the configured review policy."""
from pipeline import settings
from prospector_app.backend import caps


def test_review_descriptor_greptile(monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_PROVIDER", "greptile")
    monkeypatch.setattr(settings, "REVIEW_THRESHOLD", None)
    d = caps._review_descriptor()
    assert d == {"provider": "greptile", "label": "Greptile", "threshold": 5,
                 "score_max": 5, "retrigger": True, "stale_tracking": True}


def test_review_descriptor_none(monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_PROVIDER", "none")
    monkeypatch.setattr(settings, "REVIEW_THRESHOLD", None)
    d = caps._review_descriptor()
    assert d["provider"] == "none"
    assert d["retrigger"] is False
    assert d["stale_tracking"] is False
    assert d["threshold"] is None
