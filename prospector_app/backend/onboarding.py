"""Configuring this checkout from the app.

The ONE config write path. `worker_control` writes five lane switches and must
stay that narrow; onboarding needs the deployment target, the bot identity, and
the push identity, so it carries its own allowlist scoped by step.

Step 1's keys name the repository and the store. They are writable only while
`settings.configured()` is false, which is what stops a configured deployment
from being retargeted at another repository or another database by an API
caller. Steps 2 and 3 stay open because the wizard reaches them after step 1 has
already configured the app.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from pipeline import profile, settings
from prospector_app.backend import data, env_file

BUNDLE_VERSION = 1

PROFILE_PATH = settings.REPO_ROOT / "profile.json"

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


def _validated(step: str, updates: dict[str, str]) -> dict[str, str]:
    """`updates`, proved writable for `step`. Raises ValueError on an unknown
    step, a key outside it, a step-1 write to a configured deployment, or a
    malformed repository."""
    allowed = STEP_KEYS.get(step)
    if allowed is None:
        raise ValueError(f"not a step: {step}")
    if step == "connect" and settings.configured():
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


def apply(step: str, env: dict[str, str],
          profile_doc: dict[str, object] | None) -> dict[str, object]:
    """Write one step's configuration and adopt it in this process.

    Everything is validated before anything is written. `profile.json` goes
    first so a profile the parser would reject at boot never reaches disk; if
    the `.env` write then fails, the previous profile is put back, because a
    checkout whose policy file belongs to one deployment and whose `.env` names
    another is worse than either failure alone.
    """
    clean = _validated(step, env)
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
    try:
        env_file.write(clean)
    except OSError:
        if profile_doc is not None:
            if previous is None:
                PROFILE_PATH.unlink(missing_ok=True)
            else:
                PROFILE_PATH.write_text(previous)
        raise
    reconfigure(clean)
    return state()


def reconfigure(applied: dict[str, str]) -> None:
    """Adopt written configuration in the running process. The ONE adoption
    path: the environment, then the two things built from it at import."""
    os.environ.update(applied)
    data.reset()
    settings.default_branch.cache_clear()
    profile.reset_cache()


def build_bundle() -> dict[str, object]:
    """This deployment, as one thing a teammate can paste.

    Carries the store URL and the whole profile, because a bundle needing a
    second out-of-band step is the problem it exists to solve. It is therefore a
    credential, and the UI offering it says so.
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
    return {"version": BUNDLE_VERSION, "env": env, "profile": doc}


def parse_bundle(text: str) -> tuple[dict[str, str], dict[str, object] | None]:
    """The env mapping and profile document a bundle carries. Raises ValueError
    on anything that is not a bundle of a version this checkout reads."""
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
    return ({str(k): str(v) for k, v in env.items()},
            prof if isinstance(prof, dict) else None)


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
    if settings.configured():
        counts = {"prs": len(data.prs()), "clusters": len(data.clusters())}
        worker_ready = bool(worker_readiness.report()["ready"])
    return {
        "configured": settings.configured(),
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
