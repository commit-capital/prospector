"""The deployment target is read from the environment on each call, so a value
written to .env during onboarding takes effect without a restart."""
from __future__ import annotations

from pathlib import Path

from pipeline import settings


class TestConfigured:
    def test_true_for_a_well_formed_repo(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "octocat/hello-world")
        assert settings.configured() is True

    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        assert settings.configured() is False

    def test_false_without_an_owner(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "hello-world")
        assert settings.configured() is False


class TestAccessorsReadTheCurrentEnvironment:
    def test_repo_and_its_parts_follow_a_change(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        assert settings.repo() == "acme/widgets"
        assert settings.repo_owner() == "acme"
        assert settings.repo_name() == "widgets"
        assert settings.repo_url() == "https://github.com/acme/widgets"
        monkeypatch.setenv("TRIAGE_REPO", "other/thing")
        assert settings.repo() == "other/thing"
        assert settings.repo_owner() == "other"

    def test_unset_repo_reads_empty_rather_than_raising(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        assert settings.repo() == ""
        assert settings.repo_owner() == ""
        assert settings.repo_name() == ""

    def test_bot_login_empty_is_legal(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_BOT_LOGIN", raising=False)
        assert settings.bot_login() == ""

    def test_display_name_falls_back_to_the_repo_short_name(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.delenv("TRIAGE_DISPLAY_NAME", raising=False)
        assert settings.display_name() == "widgets"

    def test_verify_scratch_is_per_repo(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.delenv("TRIAGE_VERIFY_SCRATCH", raising=False)
        assert settings.verify_scratch() == Path.home() / ".pr-triage-verify" / "acme-widgets"
