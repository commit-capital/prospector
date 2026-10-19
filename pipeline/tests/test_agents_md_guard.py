from __future__ import annotations

import json
from pathlib import Path
import subprocess


GUARD = Path(__file__).resolve().parents[2] / ".claude/hooks/guard-agents-md.py"


def decision(command: str, cwd: Path) -> str | None:
    result = subprocess.run(
        ["python3", str(GUARD)],
        input=json.dumps({"tool_input": {"command": command}, "cwd": str(cwd)}),
        check=True,
        capture_output=True,
        text=True,
    )
    if not result.stdout:
        return None
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]


def branch_with_agents_md(path: Path, added_lines: int) -> Path:
    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
             *arguments],
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    (path / "AGENTS.md").write_text("rule\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "feature")
    (path / "AGENTS.md").write_text("rule\n" + "feature prose\n" * added_lines)
    git("commit", "-qam", "feature")
    return path


def test_asks_when_a_pr_grows_agents_md(tmp_path: Path) -> None:
    repo = branch_with_agents_md(tmp_path, added_lines=6)
    assert decision("gh pr create --fill", repo) == "ask"


def test_allows_small_growth_and_other_commands(tmp_path: Path) -> None:
    assert decision("gh pr create --fill", branch_with_agents_md(tmp_path, 5)) is None
    assert decision("git status", tmp_path) is None
