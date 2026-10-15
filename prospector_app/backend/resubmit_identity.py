"""Identity policy shared by interactive and worker-driven resubmits.

The resubmit helper owns one git/rebase implementation.  Its caller decides who
performs the push:

* interactive app chat uses the operator's existing git/SSH identity;
* the unattended autofix worker explicitly opts into the configured
  contributor-push user.

Keeping that choice in the environment (rather than a helper CLI flag) prevents
the allowlisted chat command from selecting the worker credential for itself.
"""
from __future__ import annotations

import os
import shlex

from pipeline import gh, settings
from pipeline.gh import operator_env

MACHINE_USER_ENV = "PROSPECTOR_RESUBMIT_MACHINE_USER"

# Audit marker, separate from the identity choice: "worker" when the push
# reached upstream on the automation's own judgment (a hunted action or a
# machine-approved resolve), "operator" when a person clicked or approved it.
# The activity log stamps it on each resubmit event as ``initiator``, which is
# what the Home "Done on its own" feed selects on.
INITIATOR_ENV = "PROSPECTOR_RESUBMIT_INITIATOR"


def uses_machine_user(env: dict[str, str] | None = None) -> bool:
    """Whether this invocation was explicitly launched by the autofix worker."""
    source = os.environ if env is None else env
    return source.get(MACHINE_USER_ENV) == "1"


def initiator(env: dict[str, str] | None = None) -> str:
    """Who this invocation acts for in the audit trail: "worker" only when the
    unattended marker is set, "operator" otherwise."""
    source = os.environ if env is None else env
    return "worker" if source.get(INITIATOR_ENV) == "worker" else "operator"


# GitHub's account id for a login, kept once GitHub has named it.
_account_ids: dict[str, int] = {}


def _account_id(login: str) -> int | None:
    if login not in _account_ids:
        uid = (gh.gh_json(f"users/{login}", timeout=20) or {}).get("id")
        if not isinstance(uid, int):
            return None
        _account_ids[login] = uid
    return _account_ids[login]


def commit_identity() -> tuple[str, str]:
    """The name and email a machine commit carries: the configured App's bot
    account, whose no-reply address GitHub links to the App, so a pull request
    the App opens reads as one actor and a squash merge has no machine user to
    credit; the push user when no App is configured or GitHub cannot name the
    App's account."""
    login = settings.bot_login().removesuffix("[bot]") + "[bot]"
    uid = _account_id(login) if login != "[bot]" else None
    if uid is None:
        return settings.push_login(), settings.push_email()
    return login, gh.noreply_email(uid, login)


def push_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Authenticate git as the configured contributor-push user, committing
    under `commit_identity`.

    Only the pinned key is ever offered: ``-F /dev/null`` ignores the operator's
    ssh_config (an ``IdentityFile`` it names for github.com is offered even
    under ``IdentitiesOnly``), ``IdentityAgent=none`` keeps ssh-agent out, and
    ``IdentitiesOnly`` covers the defaults.
    """
    if not settings.push_identity_configured():
        raise RuntimeError(
            "no contributor-push identity is configured on this machine: set one "
            "up on the Setup tab, or TRIAGE_PUSH_LOGIN, TRIAGE_PUSH_EMAIL and "
            "TRIAGE_PUSH_SSH_KEY_FILE in .env (see .env.example). Worker pushes "
            "never fall back to another identity.")
    key = settings.push_ssh_key_file()
    assert key is not None  # push_identity_configured() proved it
    if not key.is_file():
        raise RuntimeError(f"TRIAGE_PUSH_SSH_KEY_FILE is not a readable file: {key}")
    env = operator_env(base)
    env["GIT_SSH_COMMAND"] = (
        f"ssh -F /dev/null -i {shlex.quote(str(key))} -o IdentitiesOnly=yes "
        "-o IdentityAgent=none -o StrictHostKeyChecking=accept-new")
    name, email = commit_identity()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = email
    return env


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The selected git identity without duplicating any resubmit mechanics."""
    return push_env(base) if uses_machine_user(base) else operator_env(base)


def actor_label() -> str:
    """Human-readable actor for command output and audit explanations."""
    return settings.push_login() if uses_machine_user() else "the operator"


def worker_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Mark a resubmit subprocess as an unattended machine-user invocation."""
    env = dict(os.environ if base is None else base)
    env[MACHINE_USER_ENV] = "1"
    return env


def unattended_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """`worker_env` plus the audit marker that this push is the automation's
    own judgment — no person approved the exact change going upstream."""
    env = worker_env(base)
    env[INITIATOR_ENV] = "worker"
    return env
