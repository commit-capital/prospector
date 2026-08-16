"""The embedded agent's executor-backed PR write helpers."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

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


def test_close_pr_reads_multiline_comment_from_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    cli = _load("close-pr")
    comment_file = tmp_path / "closing-comment.md"
    comment_file.write_text("First paragraph.\n\nSecond paragraph.\n")
    seen: dict[str, object] = {}

    def close(
        pr: int,
        action: models.CloseAction,
        *,
        token: str,
        dry_run: bool,
    ) -> dict[str, object]:
        seen.update(pr=pr, action=action, token=token, dry_run=dry_run)
        return {"pr": pr, "status": "executed", "action": "CLOSE_FIXED"}

    monkeypatch.setattr(cli.executor, "mint_bot_token", lambda: "bot-token")
    monkeypatch.setattr(cli.executor, "execute_pr", close)

    assert cli.main([
        "7408", "--disposition", "fixed", "--upstream-pr", "8254",
        "--comment-file", str(comment_file),
    ]) == 0
    assert seen["action"] == models.CloseAction(
        action="CLOSE_FIXED",
        upstream_pr=8254,
        comment="First paragraph.\n\nSecond paragraph.\n",
    )
    assert json.loads(capsys.readouterr().out)["status"] == "executed"


def test_close_pr_comment_inputs_are_mutually_exclusive(tmp_path: Path) -> None:
    cli = _load("close-pr")
    comment_file = tmp_path / "closing-comment.md"
    comment_file.write_text("Closing.")

    with pytest.raises(SystemExit):
        cli.main([
            "7408", "--disposition", "fixed", "--comment", "Closing.",
            "--comment-file", str(comment_file),
        ])


def test_close_pr_comment_file_must_be_readable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = _load("close-pr")
    missing = tmp_path / "missing.md"
    monkeypatch.setattr(
        cli.executor,
        "mint_bot_token",
        lambda: (_ for _ in ()).throw(AssertionError("token mint attempted")),
    )

    with pytest.raises(SystemExit):
        cli.main([
            "7408", "--disposition", "fixed", "--comment-file", str(missing),
        ])


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
