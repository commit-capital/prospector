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


def test_malformed_hook_input_fails_closed() -> None:
    result = subprocess.run(
        ["python3", str(GUARD)],
        input="[]",
        check=True,
        capture_output=True,
        text=True,
    )
    assert denied(json.loads(result.stdout))
