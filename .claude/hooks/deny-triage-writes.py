#!/usr/bin/env python3
"""Deny direct GitHub writes to the repository configured by TRIAGE_REPO."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
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
GIT_PUSH = re.compile(r"""(?:^|[\s;&|()'"])git\s+(?:-[Cc]\s+\S+\s+)?push\b""")
CONFIG_REPO_REF = re.compile(r"\$(?:TRIAGE_REPO|\{TRIAGE_REPO\})")
SHELL_SEPARATOR = re.compile(r"&&|\|\||[;&|\n()]")
SHELL_EXPANSION = re.compile(r"[$`]")
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DIRECTORY_CHANGE = {"cd", "chdir", "pushd", "popd"}
COMMAND_PREFIXES = {"command", "env", "exec", "nice", "nohup", "sudo", "time"}
GIT_VALUE_OPTIONS = {
    "-C",
    "-c",
    "--exec-path",
    "--git-dir",
    "--namespace",
    "--work-tree",
}
PUSH_VALUE_OPTIONS = {
    "--exec",
    "-o",
    "--push-option",
    "--receive-pack",
    "--repo",
}


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


def git_output(cwd: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def cwd_repo(cwd: Path) -> str:
    return normalize_repo(git_output(cwd, "remote", "get-url", "origin"))


def push_arguments(tokens: list[str]) -> tuple[str, list[str]] | None:
    """The ``-C`` directory and post-``push`` arguments of a git push argv.

    ``None`` for anything else, including prose that merely reads like a push:
    an argv starts at its segment, behind at most environment assignments and
    wrapper commands.
    """
    index = 0
    while index < len(tokens) and (
        tokens[index] in COMMAND_PREFIXES or ENV_ASSIGNMENT.match(tokens[index])
    ):
        index += 1
    if index >= len(tokens) or Path(tokens[index]).name != "git":
        return None
    index += 1
    directory = ""
    while index < len(tokens):
        token = tokens[index]
        if token in GIT_VALUE_OPTIONS:
            if token == "-C" and index + 1 < len(tokens):
                directory = tokens[index + 1]
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(tokens) or tokens[index] != "push":
        return None
    return directory, tokens[index + 1 :]


def push_destination_argument(arguments: list[str]) -> str:
    """The repository a push names, empty when it names none."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument.startswith("--repo="):
            return argument.removeprefix("--repo=")
        if argument in PUSH_VALUE_OPTIONS:
            if argument == "--repo":
                return arguments[index + 1] if index + 1 < len(arguments) else ""
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return argument
    return ""


def default_push_remote(cwd: Path) -> str:
    branch = git_output(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    keys = (
        f"branch.{branch}.pushRemote",
        "remote.pushDefault",
        f"branch.{branch}.remote",
    )
    for key in keys:
        configured = git_output(cwd, "config", "--get", key)
        if configured:
            return configured
    return "origin"


def resolve_destination(destination: str, cwd: Path) -> str:
    """The repository a push argument writes to, empty when it cannot be resolved."""
    if SHELL_EXPANSION.search(destination):
        return ""
    if any(mark in destination for mark in ("://", ":", "/")):
        return normalize_repo(destination)
    return normalize_repo(git_output(cwd, "remote", "get-url", destination))


def push_targets(command: str, cwd: Path) -> list[tuple[str, str]] | None:
    """Each push in the command as its destination repository and own origin.

    ``None`` when a destination cannot be resolved: a segment that reads like a
    push and cannot be lexed, a directory change alongside a push, a shell
    expansion standing in for the remote, or a remote the checkout cannot name.
    """
    targets: list[tuple[str, str]] = []
    moved = False
    for part in SHELL_SEPARATOR.split(command):
        try:
            tokens = shlex.split(part)
        except ValueError:
            if GIT_PUSH.search(part):
                return None
            continue
        if tokens and tokens[0] in DIRECTORY_CHANGE:
            moved = True
            continue
        parsed = push_arguments(tokens)
        if parsed is None:
            continue
        directory, arguments = parsed
        if SHELL_EXPANSION.search(directory):
            return None
        push_cwd = cwd / directory if directory else cwd
        argument = push_destination_argument(arguments) or default_push_remote(push_cwd)
        destination = resolve_destination(argument, push_cwd)
        if not destination:
            return None
        targets.append((destination, cwd_repo(push_cwd)))
    if moved and targets:
        return None
    return targets


def push_blocked(destination: str, origin: str, *, target_repo: str) -> bool:
    """Whether a push from a checkout of ``origin`` may reach ``destination``.

    A configured target is the whole boundary. With no target configured the
    guard has no name to protect, so it holds a push to the checkout it runs in.
    """
    if target_repo:
        return destination == target_repo
    return destination != origin


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

    if GH_WRITE.search(command) and targets_configured_repo(
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

    if GIT_PUSH.search(command):
        names_target = target_repo and (
            target_repo in command or CONFIG_REPO_REF.search(command)
        )
        if names_target:
            deny(
                f"Direct pushes to {target_repo} are blocked; use the app's "
                "sanctioned, bot-identified write path."
            )
            return 0
        targets = push_targets(command, cwd)
        if targets is None:
            deny(
                "This push has no destination the guard can resolve, so it "
                "cannot be cleared against the triage write boundary."
            )
            return 0
        for destination, origin in targets:
            if not push_blocked(destination, origin, target_repo=target_repo):
                continue
            if target_repo:
                deny(
                    f"Direct pushes to {target_repo} are blocked; use the app's "
                    "sanctioned, bot-identified write path."
                )
            else:
                deny(
                    f"With no triage target configured, a push may only reach "
                    f"the checkout it runs in ({origin or 'no origin remote'}), "
                    f"not {destination}."
                )
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
