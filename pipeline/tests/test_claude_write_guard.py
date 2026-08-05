from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / ".claude/hooks/deny-triage-writes.py"


def run_guard(
    command: str,
    *,
    project_dir: Path = ROOT,
    triage_repo: str | None = "test-owner/test-repo",
    cwd: Path | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    env.pop("GH_REPO", None)
    if triage_repo is None:
        env.pop("TRIAGE_REPO", None)
    else:
        env["TRIAGE_REPO"] = triage_repo
    result = subprocess.run(
        ["python3", str(GUARD)],
        input=json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": str(cwd or project_dir),
            }
        ),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout) if result.stdout else {}


def denied(result: dict[str, Any]) -> bool:
    return (
        result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


def checkout(path: Path, **remotes: str) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for name, repo in remotes.items():
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "remote",
                "add",
                name,
                f"git@github.com:{repo}.git",
            ],
            check=True,
        )
    return path


def test_denies_literal_and_config_variable_target_writes() -> None:
    assert denied(
        run_guard("gh pr close 12 --repo test-owner/test-repo --comment done")
    )
    assert denied(run_guard('gh issue comment 12 --repo "$TRIAGE_REPO" -b done'))
    assert denied(run_guard("git push https://github.com/test-owner/test-repo.git"))


def test_allows_reads_and_writes_to_another_repo() -> None:
    assert not denied(run_guard("gh pr view 12 --repo test-owner/test-repo"))
    assert not denied(run_guard("gh pr close 12 --repo other/repo"))


def test_denies_mutating_gh_api_calls_for_every_target() -> None:
    assert denied(run_guard("gh api repos/other/repo/issues/12 -X PATCH -f state=closed"))
    assert denied(
        run_guard("gh api --method=DELETE repos/other/repo/issues/comments/1")
    )


def test_denies_target_writes_from_the_configured_checkout(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@github.com:test-owner/test-repo.git",
        ],
        check=True,
    )
    assert denied(run_guard("gh pr close 12", cwd=tmp_path))


def test_reads_target_from_gitignored_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TRIAGE_REPO=env-owner/env-repo\n")
    assert denied(
        run_guard(
            "gh issue close 7 --repo env-owner/env-repo",
            project_dir=tmp_path,
            triage_repo=None,
        )
    )


def test_missing_target_configuration_fails_closed() -> None:
    assert denied(
        run_guard(
            "gh issue create --repo any/repo --title bug --body details",
            project_dir=Path("/tmp/no-triage-config"),
            triage_repo=None,
        )
    )


def test_allows_pushing_an_unconfigured_checkout_to_its_own_origin(
    tmp_path: Path,
) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    for command in ("git push", "git push -u origin HEAD", "git push origin main"):
        assert not denied(
            run_guard(command, project_dir=tmp_path, triage_repo=None, cwd=repo)
        )


def test_denies_an_unconfigured_push_to_a_remote_beyond_the_checkout(
    tmp_path: Path,
) -> None:
    repo = checkout(
        tmp_path / "repo",
        origin="tool-owner/tool-repo",
        elsewhere="other-owner/other-repo",
    )
    assert denied(
        run_guard(
            "git push elsewhere main",
            project_dir=tmp_path,
            triage_repo=None,
            cwd=repo,
        )
    )


def test_denies_pushing_to_a_remote_that_is_the_configured_target(
    tmp_path: Path,
) -> None:
    repo = checkout(
        tmp_path / "repo",
        origin="tool-owner/tool-repo",
        upstream="test-owner/test-repo",
    )
    assert denied(run_guard("git push upstream main", cwd=repo))


def test_allows_pushing_a_configured_checkout_to_an_unrelated_origin(
    tmp_path: Path,
) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    assert not denied(run_guard("git push --force-with-lease origin HEAD", cwd=repo))
    assert not denied(run_guard("echo pushing && git push -u origin HEAD:fix", cwd=repo))


def test_denies_a_push_whose_destination_cannot_be_resolved(tmp_path: Path) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    assert denied(run_guard("git push $REMOTE main", cwd=repo))
    assert denied(run_guard("git push undeclared main", cwd=repo))


def test_resolves_the_destination_of_a_dash_c_push_from_that_directory(
    tmp_path: Path,
) -> None:
    tool = checkout(tmp_path / "tool", origin="tool-owner/tool-repo")
    target = checkout(tmp_path / "target", origin="test-owner/test-repo")
    assert not denied(run_guard(f"git -C {tool} push origin HEAD", cwd=target))
    assert denied(run_guard(f"git -C {target} push origin HEAD", cwd=tool))


def test_allows_a_command_whose_text_only_mentions_a_push(tmp_path: Path) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    assert not denied(
        run_guard("git commit -m 'stop reading every git push as a target write'", cwd=repo)
    )
    assert not denied(
        run_guard(
            "git commit -F - <<'MSG'\nEvery git push was denied by the guard\nMSG",
            cwd=repo,
        )
    )
    assert not denied(
        run_guard(
            "git commit -F - <<'MSG'\nEvery git push was denied\n"
            "The branch's remote is resolved now\nMSG",
            cwd=repo,
        )
    )


def test_allows_command_substitution_beside_a_push_mention(tmp_path: Path) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    assert not denied(
        run_guard(
            'gh pr create --title "Resolve git push destinations" '
            '--body "$(cat body.md)"',
            cwd=repo,
        )
    )


def test_reads_a_push_past_shell_redirection(tmp_path: Path) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    assert not denied(run_guard("git push 2>&1 | tail -3", cwd=repo))
    assert not denied(run_guard("git push > out.log origin HEAD", cwd=repo))
    assert denied(
        run_guard("git push 2>&1 upstream HEAD", cwd=checkout(
            tmp_path / "target", origin="test-owner/test-repo",
        ))
    )


def test_denies_a_push_whose_quoting_does_not_lex(tmp_path: Path) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    assert denied(run_guard("git push 'unclosed", cwd=repo))


def test_denies_a_push_behind_a_directory_change(tmp_path: Path) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    assert denied(run_guard("cd ../elsewhere && git push origin HEAD", cwd=repo))


def test_denies_a_push_naming_the_target_it_hides_from_the_parser(
    tmp_path: Path,
) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    assert denied(
        run_guard(
            "bash -c 'git push git@github.com:test-owner/test-repo.git HEAD:main'",
            cwd=repo,
        )
    )


def test_missing_target_configuration_still_fails_closed_for_gh_writes(
    tmp_path: Path,
) -> None:
    repo = checkout(tmp_path / "repo", origin="tool-owner/tool-repo")
    assert denied(
        run_guard("gh pr merge 12", project_dir=tmp_path, triage_repo=None, cwd=repo)
    )
    assert denied(
        run_guard(
            "gh issue close 12 --repo tool-owner/tool-repo",
            project_dir=tmp_path,
            triage_repo=None,
            cwd=repo,
        )
    )


def test_malformed_hook_input_fails_closed() -> None:
    result = subprocess.run(
        ["python3", str(GUARD)],
        input="[]",
        check=True,
        capture_output=True,
        text=True,
    )
    assert denied(json.loads(result.stdout))
