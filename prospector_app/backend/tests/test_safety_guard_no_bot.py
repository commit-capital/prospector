"""A deployment with no GitHub App writes nothing.

Writes are already inert without a mintable token, but an empty bot identity is
its own refusal: a write attributed to nobody must be refused before it reaches
the allowlist, not left to token minting to fail later.
"""
from __future__ import annotations

import pytest

from prospector_app.backend import safety_guard


def test_bot_run_refuses_without_a_bot_identity(monkeypatch):
    monkeypatch.delenv("TRIAGE_BOT_LOGIN", raising=False)
    with pytest.raises(safety_guard.WriteAttemptBlocked, match="no bot identity"):
        safety_guard.bot_run(["gh", "pr", "comment", "1", "-b", "hi"], "token-value")


def test_bot_merge_run_refuses_without_a_bot_identity(monkeypatch):
    monkeypatch.delenv("TRIAGE_BOT_LOGIN", raising=False)
    with pytest.raises(safety_guard.WriteAttemptBlocked, match="no bot identity"):
        safety_guard.bot_merge_run(["gh", "pr", "merge", "1", "--squash"], "token-value")


def test_the_empty_token_refusal_still_stands(monkeypatch):
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "acme-bot")
    with pytest.raises(safety_guard.WriteAttemptBlocked, match="without a acme-bot token"):
        safety_guard.bot_run(["gh", "pr", "comment", "1", "-b", "hi"], "")


def test_a_configured_bot_still_reaches_the_allowlist(monkeypatch):
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "acme-bot")
    with pytest.raises(safety_guard.WriteAttemptBlocked, match="not an allowlisted"):
        safety_guard.bot_run(["gh", "repo", "delete", "acme/widgets"], "token-value")
