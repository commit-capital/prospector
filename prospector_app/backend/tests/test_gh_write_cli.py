"""The embedded agent's on-demand bot write helper."""
import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
GH_WRITE = REPO_ROOT / "prospector_app" / "agent" / "gh-write"


def _load():
    loader = importlib.machinery.SourceFileLoader("gh_write_cli", str(GH_WRITE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize(("argv", "prefix"), [
    (["pr", "edit", "12", "--title", "New"], ["gh", "pr", "edit", "12"]),
    (["pr", "comment", "12", "--body", "Hi"], ["gh", "pr", "comment", "12"]),
    (["issue", "create", "--title", "Bug", "--body", "Body"],
     ["gh", "issue", "create"]),
    (["issue", "reopen", "13"], ["gh", "issue", "reopen", "13"]),
    (["issue", "comment", "13", "--body", "Hi"],
     ["gh", "issue", "comment", "13"]),
    (["issue", "edit", "13", "--body", "Body"], ["gh", "issue", "edit", "13"]),
    (["run", "rerun", "99", "--failed"], ["gh", "run", "rerun", "99"]),
])
def test_builds_only_curated_commands_and_pins_repo(argv, prefix):
    cli = _load()
    cmd = cli.build_gh_argv(argv)
    assert cmd[:len(prefix)] == prefix
    assert cmd[-2:] == ["--repo", cli.REPO]


@pytest.mark.parametrize("argv", [
    ["pr", "edit", "12", "--base", "evil"],
    ["pr", "edit", "12"],
    ["issue", "edit", "13"],
    ["issue", "close", "13"],
    ["run", "rerun", "99", "--repo", "other/repo"],
])
def test_rejects_unadvertised_operations_and_flags(argv):
    cli = _load()
    with pytest.raises(SystemExit):
        cli.build_gh_argv(argv)


def test_mints_for_each_invocation(monkeypatch, capsys):
    cli = _load()
    monkeypatch.setenv("GH_TOKEN", "expired-session-token")
    minted = []
    calls = []

    def mint():
        token = f"fresh-{len(minted) + 1}"
        minted.append(token)
        return token

    def run(argv, token):
        calls.append((argv, token))
        return SimpleNamespace(returncode=0, stdout="https://github.com/x/y/issues/1\n", stderr="")

    monkeypatch.setattr(cli.executor, "mint_bot_token", mint)
    monkeypatch.setattr(cli.safety_guard, "chat_bot_run", run)

    args = ["issue", "create", "--title", "Bug", "--body", "Body"]
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    assert [token for _, token in calls] == ["fresh-1", "fresh-2"]
    assert "expired-session-token" not in str(calls)
    assert "https://github.com/x/y/issues/1" in capsys.readouterr().out


def test_refuses_when_mint_fails(monkeypatch, capsys):
    cli = _load()
    monkeypatch.setattr(cli.executor, "mint_bot_token", lambda: None)
    monkeypatch.setattr(cli.executor, "mint_error", lambda: "key missing")
    monkeypatch.setattr(
        cli.safety_guard,
        "chat_bot_run",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("write attempted")),
    )
    assert cli.main(["pr", "comment", "12", "--body", "Hi"]) == 2
    assert "bot installation token unavailable: key missing" in capsys.readouterr().err


def test_401_is_actionable(monkeypatch, capsys):
    cli = _load()
    monkeypatch.setattr(cli.executor, "mint_bot_token", lambda: "fresh")
    monkeypatch.setattr(
        cli.safety_guard,
        "chat_bot_run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="HTTP 401: Bad credentials (https://api.github.com/graphql)",
        ),
    )
    assert cli.main(["pr", "comment", "12", "--body", "Hi"]) == 1
    assert "bot installation token expired" in capsys.readouterr().err
