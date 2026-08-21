"""Configuring this checkout from the app.

The ONE config write path. `worker_control` writes five lane switches and must
stay that narrow; onboarding needs the deployment target, the bot identity, and
the push identity, so it carries its own allowlist scoped by step.

The connect and join steps name the repository and the store. They are writable
only while `settings.configured()` is false, which is what stops a configured
deployment from being retargeted at another repository or another database by an
API caller. The writes and worker steps stay open because the wizard reaches
them after the deployment target is already configured.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

from pipeline import profile, settings
from prospector_app.backend import data, env_file

BUNDLE_VERSION = 1

PROFILE_PATH = settings.REPO_ROOT / "profile.json"

# Where a joiner files the bot's private key when a bundle carries one: under
# the user's config dir, outside the checkout and the environment, one
# subdirectory per bot login.
KEY_DIR = Path.home() / ".config" / "prospector"

_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")

# What each step of the wizard may write. A key outside its step is a hard
# error, never a silent skip.
STEP_KEYS: dict[str, tuple[str, ...]] = {
    "connect": ("TRIAGE_REPO", "TRIAGE_STORE_URL", "TRIAGE_PROFILE",
                "TRIAGE_DEFAULT_BRANCH", "TRIAGE_DISPLAY_NAME",
                "TRIAGE_REVIEW_PROVIDER", "TRIAGE_REVIEW_THRESHOLD",
                "PROSPECTOR_FEEDBACK_REPO"),
    "writes": ("TRIAGE_BOT_LOGIN", "TRIAGE_BOT_APP_ID", "TRIAGE_BOT_KEY_FILE"),
    "worker": ("TRIAGE_PUSH_LOGIN", "TRIAGE_PUSH_EMAIL", "TRIAGE_PUSH_SSH_KEY_FILE"),
    "agent": ("TRIAGE_AGENT_PROVIDER",),
}

# The agent pane's backends. The wizard also lists Codex, rendered as not
# supported yet; writing it is refused here until a backend exists.
_AGENT_PROVIDERS = ("claude", "none")

# What the bundle carries to a teammate: everything a fresh checkout needs to
# point itself at this deployment, plus the bot identity so its writes are
# attributed the same way.
_BUNDLE_KEYS = STEP_KEYS["connect"] + ("TRIAGE_BOT_LOGIN", "TRIAGE_BOT_APP_ID")

# The paste-a-bundle path writes exactly what a bundle carries.
STEP_KEYS["join"] = _BUNDLE_KEYS

# The steps that name the repository and the store, refused on a configured
# deployment so it cannot be retargeted over HTTP.
CLOSED_ONCE_CONFIGURED: tuple[str, ...] = ("connect", "join")


def _validated(step: str, updates: dict[str, str]) -> dict[str, str]:
    """`updates`, proved writable for `step`. Raises ValueError on an unknown
    step, a key outside it, a connect or join write to a configured deployment,
    or a malformed repository."""
    allowed = STEP_KEYS.get(step)
    if allowed is None:
        raise ValueError(f"not a step: {step}")
    if step in CLOSED_ONCE_CONFIGURED and settings.configured():
        raise ValueError(
            "this deployment is already configured; the repository and store "
            "are not writable from here")
    outside = sorted(set(updates) - set(allowed))
    if outside:
        raise ValueError(f"not writable in step {step}: {', '.join(outside)}")
    clean = {k: str(v).strip() for k, v in updates.items()}
    target = clean.get("TRIAGE_REPO")
    if target is not None and not _REPO_RE.match(target):
        raise ValueError(f"TRIAGE_REPO must be owner/name, not {target!r}")
    provider = clean.get("TRIAGE_AGENT_PROVIDER")
    if provider is not None and provider not in _AGENT_PROVIDERS:
        raise ValueError(
            f"TRIAGE_AGENT_PROVIDER must be one of {', '.join(_AGENT_PROVIDERS)}, "
            f"not {provider!r}")
    return clean


class Bundle(NamedTuple):
    """What a pasted bundle carries: the env mapping, the profile document, and
    the bot's private key when the sharer chose to include it."""
    env: dict[str, str]
    profile: dict[str, object] | None
    bot_key_pem: str | None


def _check_key(login: str, pem: str) -> None:
    """Raises ValueError unless `pem` is a PEM private key and `login` a name
    it can be filed under."""
    if not login:
        raise ValueError("a bundle carrying the bot key must also carry TRIAGE_BOT_LOGIN")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", login):
        raise ValueError(f"TRIAGE_BOT_LOGIN is not a name a key can be filed under: {login!r}")
    if "-----BEGIN" not in pem or "PRIVATE KEY-----" not in pem:
        raise ValueError("the bundle's bot_key_pem is not a PEM private key")


def _file_key(login: str, pem: str) -> Path:
    """Write the bot's private key under KEY_DIR for `login`, owner-only, and
    return its path."""
    folder = KEY_DIR / login
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / "private-key.pem"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pem if pem.endswith("\n") else pem + "\n")
    path.chmod(0o600)
    return path


def apply(step: str, env: dict[str, str],
          profile_doc: dict[str, object] | None,
          bot_key_pem: str | None = None) -> dict[str, object]:
    """Write one step's configuration and adopt it in this process.

    Everything is validated before anything is written. `profile.json` goes
    first so a profile the parser would reject at boot never reaches disk; if
    the `.env` write then fails, the previous profile is put back, because a
    checkout whose policy file belongs to one deployment and whose `.env` names
    another is worse than either failure alone. A bot key rides only in a
    `join` bundle: it is filed under KEY_DIR after the profile and before
    `.env` names it, and put back as it was if the `.env` write fails.
    """
    clean = _validated(step, env)
    login = clean.get("TRIAGE_BOT_LOGIN", "")
    if bot_key_pem is not None:
        if step != "join":
            raise ValueError("a bot key travels only in a pasted bundle")
        _check_key(login, bot_key_pem)
    previous: str | None = None
    if profile_doc is not None:
        # The profile parser exits the process on a bad document, which is right
        # for a CLI boot and wrong for a request handler; the refusal becomes a
        # value the caller can turn into a 400.
        try:
            profile.parse_profile(profile_doc, "onboarding bundle")
        except SystemExit as e:
            raise ValueError(str(e))
        previous = PROFILE_PATH.read_text() if PROFILE_PATH.exists() else None
        PROFILE_PATH.write_text(json.dumps(profile_doc, indent=2) + "\n")
    key_path: Path | None = None
    previous_key: str | None = None
    try:
        if bot_key_pem is not None:
            candidate = KEY_DIR / login / "private-key.pem"
            previous_key = candidate.read_text() if candidate.is_file() else None
            key_path = _file_key(login, bot_key_pem)
            clean["TRIAGE_BOT_KEY_FILE"] = str(key_path)
        env_file.write(clean)
    except OSError:
        if profile_doc is not None:
            if previous is None:
                PROFILE_PATH.unlink(missing_ok=True)
            else:
                PROFILE_PATH.write_text(previous)
        if key_path is not None:
            if previous_key is None:
                key_path.unlink(missing_ok=True)
            else:
                key_path.write_text(previous_key)
        raise
    reconfigure(clean)
    return state()


# The keys that decide which store the snapshot is built from; a write that
# moves one rebuilds the store and drops the snapshot, any other keeps it.
_STORE_KEYS = ("TRIAGE_REPO", "TRIAGE_STORE_URL")


def reconfigure(applied: dict[str, str]) -> None:
    """Adopt written configuration in the running process. The ONE adoption
    path: the environment, the two things built from it at import — the store
    snapshot only when the write moved the store — and, when the bot identity
    or its key moved, the executor's cached live probe."""
    os.environ.update(applied)
    if set(_STORE_KEYS) & set(applied):
        data.reset()
    settings.default_branch.cache_clear()
    profile.reset_cache()
    if {"TRIAGE_BOT_LOGIN", "TRIAGE_BOT_APP_ID", "TRIAGE_BOT_KEY_FILE"} & set(applied):
        from prospector_app.backend import executor
        executor.refresh_live()


def build_bundle(include_key: bool = False) -> dict[str, object]:
    """This deployment, as one thing a teammate can paste.

    Carries the store URL and the whole profile, because a bundle needing a
    second out-of-band step is the problem it exists to solve. It is therefore a
    credential, and the UI offering it says so. With `include_key`, also the
    bot's private key, read from this machine's TRIAGE_BOT_KEY_FILE, so the
    joiner's machine executes approved writes too; raises ValueError when this
    machine has no readable key or no bot login to file it under.
    """
    env = {k: os.environ[k].strip() for k in _BUNDLE_KEYS
           if os.environ.get(k, "").strip()}
    doc: dict[str, object] | None = None
    path = Path(settings.profile_path()) if settings.profile_path() else PROFILE_PATH
    if not path.is_absolute():
        path = settings.REPO_ROOT / path
    if path.is_file():
        loaded = json.loads(path.read_text())
        doc = loaded if isinstance(loaded, dict) else None
    bundle: dict[str, object] = {"version": BUNDLE_VERSION, "env": env, "profile": doc}
    if include_key:
        if not env.get("TRIAGE_BOT_LOGIN"):
            raise ValueError("no TRIAGE_BOT_LOGIN to file the key under on the other side")
        key_file = os.environ.get("TRIAGE_BOT_KEY_FILE", "").strip()
        key_path = Path(key_file).expanduser() if key_file else None
        if key_path is None or not key_path.is_file():
            raise ValueError("this machine has no readable bot key to share")
        bundle["bot_key_pem"] = key_path.read_text()
    return bundle


def parse_bundle(text: str) -> Bundle:
    """What a bundle carries. Raises ValueError on anything that is not a
    bundle of a version this checkout reads."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("not a Prospector bundle: expected JSON")
    if not isinstance(doc, dict) or "env" not in doc:
        raise ValueError("not a Prospector bundle: no env section")
    version = doc.get("version")
    if version != BUNDLE_VERSION:
        raise ValueError(
            f"this bundle is version {version}; this checkout reads version "
            f"{BUNDLE_VERSION}")
    env = doc["env"]
    if not isinstance(env, dict):
        raise ValueError("not a Prospector bundle: env is not an object")
    prof = doc.get("profile")
    pem = doc.get("bot_key_pem")
    return Bundle({str(k): str(v) for k, v in env.items()},
                  prof if isinstance(prof, dict) else None,
                  pem if isinstance(pem, str) and pem.strip() else None)


def _probe_store(url: str) -> dict[str, object]:
    """Whether this store answers, and how much is in it. A failure reports a
    category: raw exception text from a driver quotes the URL, and the URL
    carries the database password."""
    from sqlalchemy import create_engine
    from sqlalchemy import text as sql_text
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            prs = conn.execute(sql_text("SELECT count(*) FROM prs")).scalar_one()
            clusters = conn.execute(sql_text("SELECT count(*) FROM clusters")).scalar_one()
        return {"ok": True, "prs": int(prs), "clusters": int(clusters)}
    except Exception as e:
        return {"ok": False, "problem": type(e).__name__}


def _probe_repo(target: str) -> dict[str, object]:
    """Whether the operator's own gh login can read this repository."""
    from pipeline.gh import operator_env
    if not _REPO_RE.match(target.strip()):
        return {"ok": False, "problem": "not owner/name"}
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{target.strip()}", "--jq", ".full_name"],
            capture_output=True, text=True, timeout=15, env=operator_env())
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "problem": type(e).__name__}
    if r.returncode != 0:
        return {"ok": False, "problem": "gh cannot read it (missing, private, or not logged in)"}
    return {"ok": True}


def probe(store_url: str | None, repo: str | None,
          key_file: str | None, agent: bool = False) -> dict[str, object]:
    """Check candidate configuration without committing any of it.

    Diagnosing these is the wizard's job, so nothing here raises to the caller:
    an unreachable store, an unreadable repository, a missing PEM, and an
    absent or logged-out claude CLI are findings. Failures report a category,
    never raw exception text or the store URL.
    """
    found: dict[str, object] = {}
    if store_url:
        found["store"] = _probe_store(store_url)
    if repo:
        found["repo"] = _probe_repo(repo)
    if key_file:
        path = Path(key_file).expanduser()
        found["key_file"] = ({"ok": True} if path.is_file()
                             else {"ok": False, "problem": "no file at that path"})
    if agent:
        from prospector_app.backend import chat
        found["agent"] = chat.readiness()
    return found


def state() -> dict[str, object]:
    """Where this checkout stands on the setup ladder."""
    from prospector_app.backend import executor, worker_readiness

    counts: dict[str, int] = {}
    worker_ready = False
    loading = False
    if settings.configured():
        # The cold snapshot load runs on its own thread; this answer reports
        # `loading` and the caller polls, so a wizard step never holds a
        # request open for the whole load.
        loading = data.snapshot_loading()
        if not loading:
            counts = {"prs": len(data.prs()), "clusters": len(data.clusters())}
        worker_ready = bool(worker_readiness.report()["ready"])
    return {
        "configured": settings.configured(),
        "loading": loading,
        "repo": settings.repo(),
        "display_name": settings.display_name(),
        "bot_login": settings.bot_login(),
        "writes_ready": bool(settings.bot_login()) and executor.live_possible(),
        "worker_ready": worker_ready,
        # The raw choice, not the effective provider: None means the operator
        # has never picked, which is what makes the wizard's agent rung ask.
        "agent_provider": os.environ.get("TRIAGE_AGENT_PROVIDER", "").strip() or None,
        "counts": counts,
    }
