"""Identity policy shared by interactive and worker-driven resubmits.

The resubmit helper owns one git/rebase implementation.  Its caller decides who
performs the push:

* interactive app chat uses the operator's existing git/SSH identity;
* the unattended autofix worker explicitly opts into the configured machine user.

Keeping that choice in the environment (rather than a helper CLI flag) prevents
the allowlisted chat command from selecting the worker credential for itself.
"""
from __future__ import annotations

import os
import shlex

from pipeline import settings
from pipeline.gh import operator_env

MACHINE_USER_ENV = "PROSPECTOR_RESUBMIT_MACHINE_USER"


def uses_machine_user(env: dict[str, str] | None = None) -> bool:
    """Whether this invocation was explicitly launched by the autofix worker."""
    source = os.environ if env is None else env
    return source.get(MACHINE_USER_ENV) == "1"


def push_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Authenticate git as the configured worker machine user.

    The pinned key and ``IdentitiesOnly`` keep an unattended worker from falling
    through to a broader personal identity held by ssh-agent.
    """
    if not settings.push_identity_configured():
        raise RuntimeError(
            "no contributor-push identity is configured on this machine: set "
            "TRIAGE_PUSH_LOGIN, TRIAGE_PUSH_EMAIL and TRIAGE_PUSH_SSH_KEY_FILE "
            "in .env (see .env.example). Worker pushes never fall back to another "
            "identity.")
    key = settings.PUSH_SSH_KEY_FILE
    assert key is not None  # push_identity_configured() proved it
    if not key.is_file():
        raise RuntimeError(f"TRIAGE_PUSH_SSH_KEY_FILE is not a readable file: {key}")
    env = operator_env(base)
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {shlex.quote(str(key))} -o IdentitiesOnly=yes "
        "-o StrictHostKeyChecking=accept-new")
    env["GIT_AUTHOR_NAME"] = settings.PUSH_LOGIN
    env["GIT_AUTHOR_EMAIL"] = settings.PUSH_EMAIL
    env["GIT_COMMITTER_NAME"] = settings.PUSH_LOGIN
    env["GIT_COMMITTER_EMAIL"] = settings.PUSH_EMAIL
    return env


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The selected git identity without duplicating any resubmit mechanics."""
    return push_env(base) if uses_machine_user(base) else operator_env(base)


def actor_label() -> str:
    """Human-readable actor for command output and audit explanations."""
    return settings.PUSH_LOGIN if uses_machine_user() else "the operator"


def worker_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Mark a resubmit subprocess as an unattended machine-user invocation."""
    env = dict(os.environ if base is None else base)
    env[MACHINE_USER_ENV] = "1"
    return env
