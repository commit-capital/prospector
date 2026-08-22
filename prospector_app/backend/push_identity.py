"""The contributor-push user's credential, set up from the app.

The account is a GitHub user — the operator's own or a dedicated one — that
holds push on TRIAGE_REPO and authenticates by one passphrase-less SSH key
generated here for Prospector alone, filed owner-only under KEY_DIR beside a
joiner's bot key. A key is accepted only after GitHub itself names the account
it is attached to (`ssh -T`), so the login written to .env is the one that
really authenticates, whichever path the operator took to get here.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TypedDict

from pipeline.gh import operator_env
from prospector_app.backend import onboarding

KEY_NAME = onboarding.PUSH_KEY_NAME

_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
# GitHub's greeting on a successful key check: "Hi octocat! You've successfully
# authenticated, but GitHub does not provide shell access."
_GREETING_RE = re.compile(r"Hi ([A-Za-z0-9-]+)!")


class Account(TypedDict):
    login: str
    id: int
    email: str


class KeyInfo(TypedDict):
    path: str
    public_key: str


class Probe(TypedDict):
    ok: bool
    # The login GitHub greeted, when it greeted one.
    login: str | None
    problem: str | None


def noreply_email(user_id: int, login: str) -> str:
    """GitHub's per-account no-reply address, so no real address is ever
    committed."""
    return f"{user_id}+{login}@users.noreply.github.com"


def _gh_user(path: str) -> Account | None:
    try:
        r = subprocess.run(
            ["gh", "api", path, "--jq", "[.login, .id] | @tsv"],
            capture_output=True, text=True, timeout=20, env=operator_env())
    except (OSError, subprocess.SubprocessError):
        return None
    parts = r.stdout.strip().split("\t")
    if r.returncode != 0 or len(parts) != 2 or not parts[1].isdigit():
        return None
    login, uid = parts[0], int(parts[1])
    return {"login": login, "id": uid, "email": noreply_email(uid, login)}


def operator_account() -> Account | None:
    """The local gh login, or None when gh is absent or signed out."""
    return _gh_user("user")


def account(login: str) -> Account | None:
    """A GitHub user by login, or None when there is no such user or gh cannot
    ask. Public data: nothing is needed from the account itself."""
    if not _LOGIN_RE.match(login):
        return None
    return _gh_user(f"users/{login}")


def key_path(login: str) -> Path:
    return onboarding.KEY_DIR / login / KEY_NAME


def public_key(path: Path) -> str:
    """The public half of the key at `path`. Raises ValueError when ssh-keygen
    cannot read it — a missing file, or a passphrase it cannot supply."""
    try:
        r = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(path)],
            capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"could not read the key: {e}")
    if r.returncode != 0:
        raise ValueError(
            f"could not read the public key of {path} — is it a passphrase-less "
            f"private key? ({r.stderr.strip() or 'ssh-keygen failed'})")
    return r.stdout.strip()


def generate_key(login: str) -> KeyInfo:
    """An ed25519 key for `login` under KEY_DIR, owner-only and passphrase-less;
    the one already there when it exists. Raises ValueError on a login no key
    can be filed under or when ssh-keygen fails."""
    if not _LOGIN_RE.match(login):
        raise ValueError(f"not a GitHub login: {login!r}")
    path = key_path(login)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        try:
            r = subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                 f"prospector-push-{login}", "-f", str(path)],
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            raise ValueError(f"ssh-keygen failed: {e}")
        if r.returncode != 0:
            raise ValueError(f"ssh-keygen failed: {r.stderr.strip()}")
    os.chmod(path, 0o600)
    return {"path": str(path), "public_key": public_key(path)}


def read_probe(returncode: int, stderr: str, login: str) -> Probe:
    """What GitHub's answer to `ssh -T` says about the key: the account it is
    attached to, held against the `login` the operator named."""
    m = _GREETING_RE.search(stderr)
    if m is None:
        text = stderr.strip().splitlines()
        last = text[-1] if text else f"ssh exited {returncode}"
        if "Permission denied" in stderr:
            return {"ok": False, "login": None,
                    "problem": f"GitHub did not accept this key — is its public key "
                               f"added to {login}'s account yet?"}
        return {"ok": False, "login": None, "problem": f"could not reach GitHub: {last}"}
    seen = m.group(1)
    if seen.lower() != login.lower():
        return {"ok": False, "login": seen,
                "problem": f"this key is attached to {seen}, not {login}"}
    return {"ok": True, "login": seen, "problem": None}


def probe_key(path: Path, login: str) -> Probe:
    """Ask GitHub which account the key at `path` authenticates, and whether it
    is `login`. Never raises."""
    if not path.is_file():
        return {"ok": False, "login": None, "problem": f"no key at {path}"}
    try:
        # A key ssh-keygen cannot read unattended is one ssh cannot use
        # unattended either; saying so beats GitHub's bare "permission denied".
        public_key(path)
    except ValueError as e:
        return {"ok": False, "login": None, "problem": str(e)}
    # The same pinning as the worker's push: only this key is offered, so
    # GitHub's greeting names the account THIS key opens and no other.
    cmd = ["ssh", "-T", "-F", "/dev/null", "-i", str(path),
           "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
           "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=15", "git@github.com"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40,
                           stdin=subprocess.DEVNULL, env=operator_env())
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "login": None, "problem": f"could not run ssh: {e}"}
    return read_probe(r.returncode, r.stderr, login)
