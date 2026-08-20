"""The autofix worker's live-read hunt switches."""
from __future__ import annotations

from pipeline import settings


def test_fix_hunt_fix_defaults_off(monkeypatch):
    monkeypatch.delenv("TRIAGE_FIX_HUNT_FIX", raising=False)
    assert settings.fix_hunt_fix() is False


def test_fix_hunt_fix_exact_opt_in(monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "yes")
    assert settings.fix_hunt_fix() is False
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    assert settings.fix_hunt_fix() is True


def test_fix_hunt_limit_default_and_override(monkeypatch):
    monkeypatch.delenv("TRIAGE_FIX_HUNT_LIMIT", raising=False)
    assert settings.fix_hunt_limit() == 3
    monkeypatch.setenv("TRIAGE_FIX_HUNT_LIMIT", "7")
    assert settings.fix_hunt_limit() == 7
    monkeypatch.setenv("TRIAGE_FIX_HUNT_LIMIT", "junk")
    assert settings.fix_hunt_limit() == 3
    monkeypatch.setenv("TRIAGE_FIX_HUNT_LIMIT", "0")
    assert settings.fix_hunt_limit() == 3
