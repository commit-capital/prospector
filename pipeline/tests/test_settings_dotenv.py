"""settings loads the repo-root .env, real env wins, and pytest skips it."""
import os

from pipeline import settings


def test_load_env_file_fills_unset(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TRIAGE_FROM_DOTENV=yes\n")
    monkeypatch.delenv("TRIAGE_FROM_DOTENV", raising=False)
    monkeypatch.delenv("TRIAGE_SKIP_DOTENV", raising=False)
    assert settings.load_env_file(env) is True
    assert os.environ["TRIAGE_FROM_DOTENV"] == "yes"


def test_real_env_wins_over_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TRIAGE_FROM_DOTENV=fromfile\n")
    monkeypatch.setenv("TRIAGE_FROM_DOTENV", "fromenv")
    monkeypatch.delenv("TRIAGE_SKIP_DOTENV", raising=False)
    settings.load_env_file(env)
    assert os.environ["TRIAGE_FROM_DOTENV"] == "fromenv"


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
