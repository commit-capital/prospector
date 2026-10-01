"""Every environment variable the code reads is a registered setting or a
named internal one, and what the Setup page writes is validated as the code
reads it."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline import settings, settings_registry as reg

ROOT = Path(__file__).resolve().parents[2]
_PY_READS = [re.compile(p) for p in (
    r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]*)"', r'os\.getenv\(\s*"([A-Z][A-Z0-9_]*)"',
    r'os\.environ\[\s*"([A-Z][A-Z0-9_]*)"\s*\]', r'positive_int\(\s*"([A-Z][A-Z0-9_]*)"',
    r'(?:source|env|environ)\.get\(\s*"([A-Z][A-Z0-9_]*)"')]
_SH_READS = re.compile(r'\$\{?((?:TRIAGE|PROSPECTOR|PR_VERIFY|HARNESS)_[A-Z0-9_]+)')
_SKIP = (".venv", "node_modules", "/tests/", "/e2e/", "__pycache__", "/dist/", "/.git/")


def _reads() -> dict[str, str]:
    found: dict[str, str] = {}
    for f in ROOT.rglob("*"):
        path = str(f)
        if not f.is_file() or any(s in path for s in _SKIP) or f.name.startswith("test_"):
            continue
        if f.suffix == ".py" or (not f.suffix and f.parent.name == "agent"):
            pats = _PY_READS
        elif f.suffix == ".sh" or f.name == "activate":
            pats = [_SH_READS]
        else:
            continue
        text = f.read_text(errors="ignore")
        for p in pats:
            for m in p.finditer(text):
                found.setdefault(m.group(1), path)
    return found


def test_every_variable_the_code_reads_is_registered():
    unknown = {name: where for name, where in _reads().items()
               if name not in reg.BY_NAME and not reg.is_internal(name)}
    assert unknown == {}, ("register these in pipeline/settings_registry.py "
                           f"(SETTINGS or INTERNAL): {unknown}")


def test_the_scan_sees_the_settings_module():
    assert {"TRIAGE_REPO", "TRIAGE_FIX_HUNT_LIMIT", "TRIAGE_AGENT_MODEL"} <= set(_reads())


def test_nothing_editable_names_a_credential_a_path_or_the_deployment():
    for s in reg.SETTINGS:
        if s.editable:
            assert s.kind not in ("secret", "path") and s.group not in ("deployment", "advanced"), s


@pytest.mark.parametrize("name,value,want", [
    ("TRIAGE_FIX_HUNT_LIMIT", " 4 ", "4"),
    ("TRIAGE_FIX_HUNT_LIMIT", "", ""),
    ("TRIAGE_ISSUE_FIX_FOLLOWUP", "dry-run", "dry-run"),
    ("TRIAGE_ISSUE_FIX_FOLLOWUP", "", "live"),
    ("TRIAGE_AGENT_MODEL", "", "opus"),
    ("TRIAGE_FIX_HUNT_REREVIEW", "", "1"),
    ("TRIAGE_FIX_HUNT_REREVIEW", "0", "0"),
    ("TRIAGE_FIX_HUNT_SECURITY", "", ""),
    ("TRIAGE_ISSUE_FIX_MODELS", "opus, sonnet", "opus,sonnet"),
])
def test_a_written_value_reads_back_as_meant(name, value, want):
    assert reg.validate(name, value) == want


@pytest.mark.parametrize("name,value,why", [
    ("TRIAGE_FIX_HUNT_LIMIT", "lots", "whole number"),
    ("TRIAGE_FIX_HUNT_LIMIT", "0", "1 or more"),
    ("TRIAGE_FIX_AUTOPUSH_MIN_TIER", "4", "1 to 3"),
    ("TRIAGE_ISSUE_FIX_FOLLOWUP", "sometimes", "one of"),
    ("TRIAGE_FIX_HUNT_SECURITY", "yes", "'1'"),
    ("TRIAGE_FIX_HUNT_REREVIEW", "yes", "'1' or '0'"),
    ("TRIAGE_ISSUE_FIX_MODELS", "opus; rm -rf", "comma-separated"),
])
def test_a_value_the_code_would_misread_is_refused(name, value, why):
    with pytest.raises(ValueError, match=why):
        reg.validate(name, value)


@pytest.mark.parametrize("name,value,fn,want", [
    ("TRIAGE_FIX_HUNT_REREVIEW", "1", settings.fix_hunt_rereview, True),
    ("TRIAGE_FIX_HUNT_REREVIEW", "0", settings.fix_hunt_rereview, False),
    ("TRIAGE_ISSUE_FIX_FOLLOWUP", "live", settings.issue_fix_followup, "live"),
    ("TRIAGE_AGENT_MODEL", "opus", settings.agent_model, "opus"),
    ("TRIAGE_REVIEWER_ACTIVE_DAYS", "", settings.reviewer_active_days, 14),
])
def test_the_code_reads_a_validated_value_as_meant(monkeypatch, name, value, fn, want):
    monkeypatch.setenv(name, reg.validate(name, value))
    assert fn() == want


def test_a_bad_integer_reads_as_the_default(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEWER_ACTIVE_DAYS", "two weeks")
    monkeypatch.setenv("TRIAGE_REVIEW_THRESHOLD", "high")
    assert settings.reviewer_active_days() == 14 and settings.review_threshold() is None
