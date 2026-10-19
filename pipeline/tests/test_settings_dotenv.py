"""settings loads the repo-root .env, the file wins, and pytest skips it."""
import os

from pipeline import settings


def test_load_env_file_fills_unset(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TRIAGE_FROM_DOTENV=yes\n")
    monkeypatch.delenv("TRIAGE_FROM_DOTENV", raising=False)
    monkeypatch.delenv("TRIAGE_SKIP_DOTENV", raising=False)
    assert settings.load_env_file(env) is True
    assert os.environ["TRIAGE_FROM_DOTENV"] == "yes"


def test_file_wins_over_a_stale_inherited_value(tmp_path, monkeypatch):
    """A server restarted by `serve --dev`'s reloader inherits the blank its
    launcher loaded; the lane switched on in .env since must read on."""
    env = tmp_path / ".env"
    env.write_text("TRIAGE_ISSUE_FIX_WORKER=1\n")
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_WORKER", "")
    monkeypatch.delenv("TRIAGE_SKIP_DOTENV", raising=False)
    settings.load_env_file(env)
    assert settings.issue_fix_worker_enabled()


def test_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIAGE_SKIP_DOTENV", raising=False)
    assert settings.load_env_file(tmp_path / "nope.env") is False


def test_skip_flag_disables_loading(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TRIAGE_FROM_DOTENV=should_not_load\n")
    monkeypatch.delenv("TRIAGE_FROM_DOTENV", raising=False)
    monkeypatch.setenv("TRIAGE_SKIP_DOTENV", "1")
    assert settings.load_env_file(env) is False
    assert "TRIAGE_FROM_DOTENV" not in os.environ
