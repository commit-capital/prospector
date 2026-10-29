"""The bot's installation token is minted by get-bot-token.sh, reused while it
has more than MARGIN_SECONDS of life, and re-minted inside that margin or after
forget(). A failed mint is never cached, and last_error says why it failed."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from pipeline import bot_token

REAL_GET = bot_token.get


@pytest.fixture(autouse=True)
def real_get(monkeypatch):
    monkeypatch.setattr(bot_token, "get", REAL_GET)
    monkeypatch.setattr(bot_token, "_token", None)
    monkeypatch.setattr(bot_token, "_last_error", None)


def _script(monkeypatch, stdout: str = "", stderr: str = "", returncode: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    monkeypatch.setattr(bot_token.subprocess, "run", fake_run)
    return calls


def _clock(monkeypatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(bot_token.time, "monotonic", lambda: now[0])
    return now


def test_a_token_is_reused_until_only_the_margin_is_left(monkeypatch):
    calls = _script(monkeypatch, stdout="ghs_first\n")
    now = _clock(monkeypatch)
    assert bot_token.get() == "ghs_first"
    now[0] += bot_token.LIFETIME_SECONDS - bot_token.MARGIN_SECONDS - 1
    assert bot_token.get() == "ghs_first"
    assert len(calls) == 1

    now[0] += 1
    bot_token.get()
    assert len(calls) == 2


def test_forget_drops_the_cached_token(monkeypatch):
    calls = _script(monkeypatch, stdout="ghs_tok\n")
    bot_token.get()
    bot_token.forget()
    bot_token.get()
    assert len(calls) == 2


def test_a_failed_mint_is_retried_on_the_next_call(monkeypatch):
    calls = _script(monkeypatch, stderr="ERROR: GitHub App 123 has no installation on test-owner.\n",
                    returncode=1)
    assert bot_token.get() is None
    assert bot_token.get() is None
    assert len(calls) == 2
    assert not bot_token.available()


def test_last_error_surfaces_the_script_stderr(monkeypatch):
    _script(monkeypatch, stderr="ERROR: TRIAGE_BOT_KEY_FILE is required. Set it in .env.\n",
            returncode=1)
    assert bot_token.get() is None
    assert "TRIAGE_BOT_KEY_FILE" in (bot_token.last_error() or "")


def test_last_error_falls_back_to_the_exit_code_with_no_stderr(monkeypatch):
    _script(monkeypatch, returncode=7)
    assert bot_token.get() is None
    assert "exited 7" in (bot_token.last_error() or "")


def test_last_error_names_a_missing_script(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_token, "GET_TOKEN", tmp_path / "nonexistent-script.sh")
    assert bot_token.get() is None
    assert "not found" in (bot_token.last_error() or "")


def test_last_error_hides_a_helper_exception(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="get-bot-token.sh", timeout=30)
    monkeypatch.setattr(bot_token.subprocess, "run", boom)
    assert bot_token.get() is None
    assert bot_token.last_error() == "token helper failed unexpectedly; see server logs"


def test_a_successful_mint_clears_the_prior_error(monkeypatch):
    bot_token._last_error = "stale reason from a previous failed probe"
    _script(monkeypatch, stdout="ghs_realtoken\n")
    assert bot_token.get() == "ghs_realtoken"
    assert bot_token.last_error() is None


def test_token_script_reads_generic_key_path_config(tmp_path):
    missing = tmp_path / "missing-key.pem"
    env = {
        **os.environ,
        "TRIAGE_REPO": "example/repo",
        "TRIAGE_BOT_APP_ID": "123",
        "TRIAGE_BOT_KEY_FILE": str(missing),
    }
    r = subprocess.run(["bash", str(bot_token.GET_TOKEN)], capture_output=True,
                       text=True, timeout=10, env=env)
    assert r.returncode != 0
    assert f"TRIAGE_BOT_KEY_FILE is not readable: {missing}" in r.stderr


def test_token_script_permissions_mode_returns_metadata_without_token(tmp_path):
    key = tmp_path / "private-key.pem"
    key.write_text("test-key")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\nprintf 'test-jwt\\n'\n")
    node.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text("""#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    */access_tokens)
      printf '%s\\n' '{"token":"installation-secret","permissions":{"actions":"write","issues":"write"}}'
      exit 0
      ;;
  esac
done
printf '%s\\n' '[{"id":42,"account":{"login":"example"}}]'
""")
    curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TRIAGE_REPO": "example/repo",
        "TRIAGE_BOT_APP_ID": "123",
        "TRIAGE_BOT_KEY_FILE": str(key),
    }

    result = subprocess.run(
        ["bash", str(bot_token.GET_TOKEN), "--permissions"],
        capture_output=True, text=True, timeout=10, env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '{"actions":"write","issues":"write"}'
    assert "installation-secret" not in result.stdout
