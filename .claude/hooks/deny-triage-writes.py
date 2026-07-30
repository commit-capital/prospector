#!/usr/bin/env python3
"""Deny direct GitHub writes to the repository configured by TRIAGE_REPO."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse


GH_WRITE = re.compile(
    r"(?:^|[\s;&|()])(?:[^\s;&|()]*/)?gh\s+"
    r"(?:pr\s+(?:merge|close|reopen|comment|edit|review|ready)"
    r"|issue\s+(?:create|edit|close|reopen|comment|delete)"
    r"|release\b)"
)
GH_API_WRITE = re.compile(
    r"(?:^|[\s;&|()])(?:[^\s;&|()]*/)?gh\s+api\b"
    r".*(?:-X\s*|--method(?:=|\s+))(?:POST|PATCH|PUT|DELETE)\b",
    re.IGNORECASE | re.DOTALL,
)
GIT_PUSH = re.compile(r"(?:^|[\s;&|()])git\s+(?:-[Cc]\s+\S+\s+)?push\b")
CONFIG_REPO_REF = re.compile(r"\$(?:TRIAGE_REPO|\{TRIAGE_REPO\})")


def env_file_value(project_dir: Path, name: str) -> str:
    path = project_dir / ".env"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    prefix = f"{name}="
    values = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
    return values[-1] if values else ""


def configured_value(project_dir: Path, name: str) -> str:
    return os.environ.get(name, "") or env_file_value(project_dir, name)


def normalize_repo(remote: str) -> str:
    value = remote.strip()
    if value.startswith("git@") and ":" in value:
        value = value.split(":", 1)[1]
    elif "://" in value:
        value = urlparse(value).path.lstrip("/")
    return value.removesuffix(".git").strip("/")


def cwd_repo(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return normalize_repo(result.stdout)


def targets_configured_repo(
    command: str,
    *,
    target_repo: str,
    cwd: Path,
) -> bool:
    if not target_repo:
        return True
    if target_repo in command or CONFIG_REPO_REF.search(command):
        return True
    if os.environ.get("GH_REPO") == target_repo:
        return True
    return cwd_repo(cwd) == target_repo


def deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        deny("Cannot validate this Bash command against the triage write boundary.")
        return 0

    if not isinstance(event, dict):
        deny("Cannot validate this Bash command against the triage write boundary.")
        return 0
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        deny("Cannot validate this Bash command against the triage write boundary.")
        return 0
    command = tool_input.get("command", "")
    if not isinstance(command, str):
        deny("Cannot validate this Bash command against the triage write boundary.")
        return 0

    project_dir = Path(
        os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd()
    )
    cwd = Path(event.get("cwd") or project_dir)
    target_repo = configured_value(project_dir, "TRIAGE_REPO")

    if GH_API_WRITE.search(command):
        deny("Direct mutating gh api calls are outside the app write boundary.")
        return 0

    is_direct_write = GH_WRITE.search(command) or GIT_PUSH.search(command)
    if is_direct_write and targets_configured_repo(
        command,
        target_repo=target_repo,
        cwd=cwd,
    ):
        target = target_repo or "the unconfigured triage target"
        deny(
            f"Direct writes to {target} are blocked; use the app's "
            "sanctioned, bot-identified write path."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
