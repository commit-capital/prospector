"""The embedded agent's executor-backed PR write helpers."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

from prospector_app.backend import models

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = REPO_ROOT / "prospector_app" / "agent"


def _load(name):
    path = AGENT_DIR / name
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_close_pr_uses_executor_with_full_disposition(monkeypatch, capsys):
    cli = _load("close-pr")
    seen = {}

    def close(pr, action, *, token, dry_run):
        seen.update(pr=pr, action=action, token=token, dry_run=dry_run)
        return {"pr": pr, "status": "executed", "action": "CLOSE_DUP"}

    monkeypatch.setattr(cli.executor, "mint_bot_token", lambda: "bot-token")
    monkeypatch.setattr(cli.executor, "execute_pr", close)
    rc = cli.main([
        "2857", "--disposition", "dup", "--canonical", "2800",
        "--dup-reason", "it has broader coverage", "--comment", "Closing this.",
    ])

    assert rc == 0
    assert seen["pr"] == 2857
    assert seen["token"] == "bot-token" and seen["dry_run"] is False
    assert seen["action"] == models.CloseAction(
        action="CLOSE_DUP",
        canonical=2800,
        dup_reason="it has broader coverage",
        comment="Closing this.",
    )
    assert json.loads(capsys.readouterr().out)["status"] == "executed"


def test_close_pr_refuses_when_a_fresh_bot_token_cannot_be_minted(monkeypatch, capsys):
    cli = _load("close-pr")
    monkeypatch.setattr(cli.executor, "mint_bot_token", lambda: None)
    monkeypatch.setattr(cli.executor, "mint_error", lambda: "key missing")
    monkeypatch.setattr(
        cli.executor,
        "execute_pr",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executor called")),
    )

    assert cli.main(["2857", "--disposition", "manual", "--comment", "Closing."]) == 2
    assert "bot installation token unavailable: key missing" in capsys.readouterr().err


def test_reopen_pr_uses_executor(monkeypatch, capsys):
    cli = _load("reopen-pr")
    seen = {}

    def reopen(pr, *, token, dry_run):
        seen.update(pr=pr, token=token, dry_run=dry_run)
        return {"pr": pr, "status": "reopened", "action": "REOPEN"}

    monkeypatch.setattr(cli.executor, "mint_bot_token", lambda: "bot-token")
    monkeypatch.setattr(cli.executor, "reopen_pr", reopen)

    assert cli.main(["2857"]) == 0
    assert seen == {"pr": 2857, "token": "bot-token", "dry_run": False}
    assert json.loads(capsys.readouterr().out)["status"] == "reopened"


def test_submit_review_uses_executor(monkeypatch, capsys):
    cli = _load("submit-review")
    seen = {}

    def review(pr, event, body, *, token, dry_run):
        seen.update(pr=pr, event=event, body=body, token=token, dry_run=dry_run)
        return {"pr": pr, "status": "executed", "action": f"REVIEW:{event}"}

    monkeypatch.setattr(cli.executor, "mint_bot_token", lambda: "bot-token")
    monkeypatch.setattr(cli.executor, "submit_review", review)

    assert cli.main([
        "2857", "--event", "request-changes", "--body", "Please add a test.",
    ]) == 0
    assert seen == {
        "pr": 2857,
        "event": "request-changes",
        "body": "Please add a test.",
        "token": "bot-token",
        "dry_run": False,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "executed"


def test_executor_rejection_returns_failure_and_prints_result(monkeypatch, capsys):
    cli = _load("submit-review")
    monkeypatch.setattr(cli.executor, "mint_bot_token", lambda: "bot-token")
    monkeypatch.setattr(
        cli.executor,
        "submit_review",
        lambda *args, **kwargs: {"status": "skipped", "detail": "already closed"},
    )

    assert cli.main(["2857", "--event", "approve"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "skipped"
