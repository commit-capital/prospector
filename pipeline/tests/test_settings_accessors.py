"""The settings defaults that keep a checkout or a test process from acting on
a deployment it was not pointed at."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import settings


@pytest.mark.parametrize("repo, configured", [
    ("octocat/hello-world", True), (None, False), ("hello-world", False)])
def test_a_checkout_without_a_well_formed_target_is_unconfigured(monkeypatch, repo, configured):
    if repo is None:
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
    else:
        monkeypatch.setenv("TRIAGE_REPO", repo)
    assert settings.configured() is configured
    if repo is None:
        assert settings.repo() == settings.repo_owner() == settings.repo_name() == ""


def test_a_test_process_scratches_outside_the_machines_verify_directory():
    # Parallel test processes writing one directory read each other's files.
    assert not settings.verify_scratch().is_relative_to(Path.home() / ".pr-triage-verify")
