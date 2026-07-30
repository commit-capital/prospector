"""Deployment target — the one place the repo/bot identity lives.

Every pipeline and app module reads these from here instead of re-declaring a
literal, so pointing the whole system at a different repository is a matter of
setting a few environment variables rather than editing a dozen files. No identity
is baked into the source: TRIAGE_REPO and TRIAGE_BOT_LOGIN are required (a clear
error if unset). Set them all in .env — see .env.example.
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# The repo-root .env is the single environment source for the whole system
# (Python here, plus setup.sh and Vite). Load it before reading any value
# so a CLI run picks up the same config the app does. Real environment
# variables win over the file (override=False), and the path is resolved relative
# to this file so it works regardless of the current working directory.
# TRIAGE_SKIP_DOTENV (set by the test conftests) disables loading so the suite is
# hermetic and never adopts a developer's real .env (e.g. a live database URL).
REPO_ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path = REPO_ROOT / ".env") -> bool:
    """Load `path` into os.environ without overriding already-set vars. Returns
    True if the file was read, False if it was absent or skipped via
    TRIAGE_SKIP_DOTENV."""
    if os.environ.get("TRIAGE_SKIP_DOTENV"):
        return False
    return load_dotenv(path, override=False, interpolate=False)


load_env_file()

# "owner/name" of the upstream repository being triaged. Read-only fetches and
# every upstream write target this repo. Required — there is no default, so the
# system can never silently act against the wrong repo.
_repo = os.environ.get("TRIAGE_REPO")
if not _repo or "/" not in _repo:
    raise SystemExit(
        "TRIAGE_REPO is required and must be 'owner/name' (e.g. octocat/hello-world). "
        "Set it in .env — see .env.example."
    )
REPO: str = _repo
REPO_OWNER, REPO_NAME = REPO.split("/", 1)

# GitHub web URL of the upstream repository — the base every PR/issue/blob link
# is built from.
REPO_URL: str = f"https://github.com/{REPO}"

# Human-facing product name for the app (tab title, headings). Defaults to
# the repository's short name.
DISPLAY_NAME: str = os.environ.get("TRIAGE_DISPLAY_NAME") or REPO_NAME

# "owner/name" the app's 🐞 Feedback button files issues into. Empty disables
# the button — feedback about this tool must never land on the triaged upstream.
FEEDBACK_REPO: str = os.environ.get("PROSPECTOR_FEEDBACK_REPO", "")


@lru_cache(maxsize=1)
def default_branch() -> str:
    """The upstream repository's default branch. TRIAGE_DEFAULT_BRANCH wins when
    set; otherwise discovered once from GitHub via `gh` and cached for the
    process. Falls back to "main" when discovery is unavailable (offline, no gh
    auth)."""
    configured = os.environ.get("TRIAGE_DEFAULT_BRANCH")
    if configured:
        return configured
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{REPO}", "--jq", ".default_branch"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "main"
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else "main"

# Login of the GitHub App identity the app executes upstream writes as.
# Required — writes must be attributed to a known identity.
_bot_login = os.environ.get("TRIAGE_BOT_LOGIN")
if not _bot_login:
    raise SystemExit(
        "TRIAGE_BOT_LOGIN is required (the GitHub App login that upstream writes "
        "post as). Set it in .env — see .env.example."
    )
BOT_LOGIN: str = _bot_login

# SQLAlchemy URL for the backing store. Unset → each Store falls back to a local
# SQLite file under its root (dev, CI, OSS-solo). Set it to a shared PostgreSQL
# database (e.g. postgresql+psycopg://…) to point the whole team at one store.
# SQLite and PostgreSQL are the supported store dialects.
STORE_URL: str | None = os.environ.get("TRIAGE_STORE_URL") or None

# When "1", a checkout whose schema.STORE_SCHEMA_VERSION is behind the store's
# stamp may still write — the guard's refusal downgrades to a stderr warning.
# Emergency escape hatch; leave unset normally.
STORE_ALLOW_STALE: bool = os.environ.get("TRIAGE_STORE_ALLOW_STALE", "") == "1"

# When "1", a process whose REPO differs from the store's stamped repo may still
# write to the activity log — the guard's refusal downgrades to a stderr warning.
# Emergency escape hatch; leave unset normally.
STORE_ALLOW_FOREIGN_REPO: bool = os.environ.get("TRIAGE_STORE_ALLOW_FOREIGN_REPO", "") == "1"

# Host scratch root for the VERIFY sandbox — base clones, patches, exclusion
# sets, and the suite config all live under it. Tilde-expanded. Defaults to a
# per-repo directory so two triaged repositories on one machine never share
# base trees. Must live under $HOME on macOS+Colima: Colima's virtiofs shares
# only $HOME, so a path outside it is invisible to the VM that runs Docker.
_verify_scratch = os.environ.get("TRIAGE_VERIFY_SCRATCH", "")
VERIFY_SCRATCH: Path = (
    Path(_verify_scratch).expanduser() if _verify_scratch
    else Path.home() / ".pr-triage-verify" / f"{REPO_OWNER}-{REPO_NAME}")

# Comma-separated host:port entries the sandbox boot probe must FAIL to reach
# (this machine's sensitive host services, e.g. a credentialed local server).
# Empty keeps sandbox/boot-probe.sh's built-in default list.
VERIFY_PROBE_DENY: str = os.environ.get("TRIAGE_VERIFY_PROBE_DENY", "")

# Repository policy profile — path to a JSON file carrying the triaged
# repository's policy vocabulary (pipeline/profile.py owns the schema and
# validation). Relative paths resolve against the repo root. Empty selects the
# built-in generic default profile.
PROFILE_PATH: str = os.environ.get("TRIAGE_PROFILE", "")


def parse_review_provider(raw: str | None) -> str:
    """The configured external code-review provider, normalised. "greptile"
    reproduces the confidence-score merge bar; "none" requires no external review
    (a repository with no such provider). Unknown values are a hard error so a
    typo never silently disables the bar."""
    provider = (raw or "none").strip().lower()
    if provider not in ("greptile", "none"):
        raise SystemExit(
            f"TRIAGE_REVIEW_PROVIDER must be 'greptile' or 'none' (got {provider!r}). "
            "Set it in .env — see .env.example."
        )
    return provider


# External code-review provider whose verdict gates a clean merge (see
# pipeline/review_policy.py for the profiles). Defaults to "none" so a fresh
# checkout runs without assuming any provider.
REVIEW_PROVIDER: str = parse_review_provider(os.environ.get("TRIAGE_REVIEW_PROVIDER"))

# Optional override of the review provider's pass threshold. Unset → the
# provider's built-in bar (greptile → 5).
_review_threshold = os.environ.get("TRIAGE_REVIEW_THRESHOLD")
REVIEW_THRESHOLD: int | None = int(_review_threshold) if _review_threshold else None
