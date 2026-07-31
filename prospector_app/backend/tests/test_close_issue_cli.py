"""The embedded agent's executor-backed issue-close helper."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

from prospector_app.backend import models

REPO_ROOT = Path(__file__).resolve().parents[3]
CLOSE_ISSUE = REPO_ROOT / "prospector_app" / "agent" / "close-issue"


def _load():
    loader = importlib.machinery.SourceFileLoader("close_issue_cli", str(CLOSE_ISSUE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_close_uses_executor_with_injected_bot_token(monkeypatch, capsys):
    cli = _load()
    seen = {}

    def close(issue, action, *, token, dry_run):
        seen.update(issue=issue, action=action, token=token, dry_run=dry_run)
        return {"issue": issue, "status": "executed", "action": "CLOSE_ISSUE_DUP"}

    monkeypatch.setenv("GH_TOKEN", "bot-token")
    monkeypatch.setattr(cli.executor, "close_issue_with_comment", close)
    rc = cli.main(["1883", "--disposition", "dup", "--canonical", "1800"])

    assert rc == 0
    assert seen["issue"] == 1883
    assert seen["token"] == "bot-token" and seen["dry_run"] is False
    assert seen["action"] == models.IssueCloseBody(disposition="dup", canonical=1800)
    assert json.loads(capsys.readouterr().out)["status"] == "executed"


def test_close_refuses_without_the_injected_bot_token(monkeypatch, capsys):
    cli = _load()
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(
        cli.executor,
        "close_issue_with_comment",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executor called")),
    )
    assert cli.main(["1883", "--disposition", "completed", "--comment", "done"]) == 2
    assert "no bot token" in capsys.readouterr().err


def test_blocked_close_returns_failure_and_prints_the_result(monkeypatch, capsys):
    cli = _load()
    monkeypatch.setenv("GH_TOKEN", "bot-token")
    monkeypatch.setattr(
        cli.executor,
        "close_issue_with_comment",
        lambda *args, **kwargs: {"status": "blocked", "detail": "already closed"},
    )
    assert cli.main(["1883", "--disposition", "not-planned", "--comment", "stale"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
