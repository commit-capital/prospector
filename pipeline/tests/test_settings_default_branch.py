"""settings.default_branch(): explicit config wins, GitHub discovery runs once
per process, and any discovery failure falls back to "main"."""
from __future__ import annotations

import subprocess

import pytest

from pipeline import settings


@pytest.fixture(autouse=True)
def _fresh_cache():
    """default_branch is lru_cached for the process; clear it around each test
    so env/subprocess monkeypatching actually takes effect, and again after so
    later tests re-cache from the conftest-pinned TRIAGE_DEFAULT_BRANCH."""
    settings.default_branch.cache_clear()
    yield
    settings.default_branch.cache_clear()


def test_configured_branch_wins_without_shelling_out(monkeypatch):
    monkeypatch.setenv("TRIAGE_DEFAULT_BRANCH", "develop")

    def boom(*_a, **_k):
        raise AssertionError("must not call gh when the branch is configured")

    monkeypatch.setattr(subprocess, "run", boom)
    assert settings.default_branch() == "develop"


def test_discovers_from_github_and_caches(monkeypatch):
    monkeypatch.delenv("TRIAGE_DEFAULT_BRANCH", raising=False)
    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "release\n"

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert settings.default_branch() == "release"
    assert settings.default_branch() == "release"
    assert len(calls) == 1
    assert f"repos/{settings.REPO}" in calls[0]


def test_falls_back_to_main_when_gh_is_unavailable(monkeypatch):
    monkeypatch.delenv("TRIAGE_DEFAULT_BRANCH", raising=False)

    def boom(*_a, **_k):
        raise OSError("gh not installed")

    monkeypatch.setattr(subprocess, "run", boom)
    assert settings.default_branch() == "main"


def test_falls_back_to_main_when_gh_errors(monkeypatch):
    monkeypatch.delenv("TRIAGE_DEFAULT_BRANCH", raising=False)

    class Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: Failed())
    assert settings.default_branch() == "main"
