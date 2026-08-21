"""The in-app agent's provider setting and readiness probe.

The agent pane runs the operator's local `claude` CLI under their own login,
so readiness is a per-machine fact: the binary on PATH and its auth status.
`settings.agent_provider()` is the deployment-side switch the pane hides on.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from pipeline import settings
from prospector_app.backend import chat


class TestAgentProvider:
    def test_unset_reads_as_claude(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_AGENT_PROVIDER", raising=False)
        assert settings.agent_provider() == "claude"

    def test_none_reads_as_none(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "none")
        assert settings.agent_provider() == "none"

    def test_claude_reads_as_claude(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "claude")
        assert settings.agent_provider() == "claude"

    def test_an_unrecognized_value_fails_toward_no_agent(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "codex")
        assert settings.agent_provider() == "none"


def _auth_status(payload: dict[str, object], returncode: int = 0):
    """A fake subprocess.run for `claude auth status --json`."""
    def run(argv, **kwargs):
        assert argv[1:] == ["auth", "status", "--json"]
        return subprocess.CompletedProcess(
            argv, returncode, stdout=json.dumps(payload), stderr="")
    return run


class TestReadiness:
    @pytest.fixture(autouse=True)
    def _claude_provider(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "claude")

    def test_provider_none_is_not_ready(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "none")
        found = chat.readiness()
        assert found["provider"] == "none"
        assert found["ok"] is False

    def test_missing_binary_names_the_problem(self, monkeypatch):
        monkeypatch.setattr(chat.shutil, "which", lambda name: None)
        found = chat.readiness()
        assert found["ok"] is False
        assert found["problem"] == "claude CLI not on PATH"

    def test_logged_out_cli_is_not_ready(self, monkeypatch):
        monkeypatch.setattr(chat.shutil, "which", lambda name: "/usr/bin/claude")
        monkeypatch.setattr(chat.subprocess, "run", _auth_status({"loggedIn": False}))
        found = chat.readiness()
        assert found["ok"] is False
        assert found["problem"] == "not logged in"

    def test_logged_in_cli_is_ready_with_auth_details(self, monkeypatch):
        monkeypatch.setattr(chat.shutil, "which", lambda name: "/usr/bin/claude")
        monkeypatch.setattr(chat.subprocess, "run", _auth_status(
            {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))
        found = chat.readiness()
        assert found == {"provider": "claude", "ok": True,
                         "auth_method": "claude.ai", "subscription": "max"}

    def test_a_failing_status_command_reports_a_category(self, monkeypatch):
        monkeypatch.setattr(chat.shutil, "which", lambda name: "/usr/bin/claude")
        def boom(argv, **kwargs):
            raise OSError("exec format error")
        monkeypatch.setattr(chat.subprocess, "run", boom)
        found = chat.readiness()
        assert found["ok"] is False
        assert found["problem"] == "OSError"

    def test_unparseable_status_output_is_not_ready(self, monkeypatch):
        monkeypatch.setattr(chat.shutil, "which", lambda name: "/usr/bin/claude")
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")
        monkeypatch.setattr(chat.subprocess, "run", run)
        found = chat.readiness()
        assert found["ok"] is False
        assert found["problem"] == "unrecognized auth status"


class TestSurfaces:
    def test_the_ready_route_serves_the_probe(self, monkeypatch):
        from fastapi.testclient import TestClient
        from prospector_app.backend import app as app_mod
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setattr(chat, "readiness",
                            lambda: {"provider": "claude", "ok": True})
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        r = client.get("/api/chat/ready")
        assert r.status_code == 200
        assert r.json() == {"provider": "claude", "ok": True}

    def test_meta_carries_the_effective_provider(self, monkeypatch):
        from prospector_app.backend import repo_meta
        monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "none")
        assert repo_meta.meta()["agent_provider"] == "none"

    def test_meta_defaults_the_provider_to_claude(self, monkeypatch):
        from prospector_app.backend import repo_meta
        monkeypatch.delenv("TRIAGE_AGENT_PROVIDER", raising=False)
        assert repo_meta.meta()["agent_provider"] == "claude"
