"""Deployment target — the one place the repo/bot identity lives.

Every pipeline and app module reads these from here instead of re-declaring a
literal, so pointing the whole system at a different repository is a matter of
setting a few environment variables rather than editing a dozen files. No identity
is baked into the source. Set them in .env — see .env.example.

Each value is read from the environment on the call, so configuration written to
.env while the app runs takes effect without a restart. `configured()` answers
whether this checkout has a deployment target at all; a checkout without one
reads empty everywhere and its API refuses to serve.
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

def configured() -> bool:
    """Whether this checkout has a deployment target. The ONE predicate the
    unconfigured gate and the onboarding wizard both read."""
    return "/" in os.environ.get("TRIAGE_REPO", "")


def repo() -> str:
    """"owner/name" of the upstream repository being triaged. Read-only fetches
    and every upstream write target it. Empty until the deployment is
    configured, which makes those calls fail rather than reach another repo."""
    return os.environ.get("TRIAGE_REPO", "")


def repo_owner() -> str:
    target = repo()
    return target.split("/", 1)[0] if "/" in target else ""


def repo_name() -> str:
    target = repo()
    return target.split("/", 1)[1] if "/" in target else ""


def repo_url() -> str:
    """GitHub web URL of the upstream repository — the base every PR/issue/blob
    link is built from."""
    return f"https://github.com/{repo()}"


def display_name() -> str:
    """Human-facing product name for the app (tab title, headings). Defaults to
    the repository's short name."""
    return os.environ.get("TRIAGE_DISPLAY_NAME") or repo_name()


def agent_provider() -> str:
    """Which AI backs the app's in-chat agent pane: "claude" (the operator's
    local Claude Code CLI, under their own login) or "none" (no agent pane).
    Defaults to "claude"; an unrecognized value reads as "none", failing toward
    no agent rather than spawning a CLI the operator did not pick."""
    raw = os.environ.get("TRIAGE_AGENT_PROVIDER", "").strip().lower()
    if not raw:
        return "claude"
    return raw if raw in ("claude", "none") else "none"


def feedback_repo() -> str:
    """"owner/name" the app's 🐞 Feedback button files issues into. Empty
    disables the button — feedback about this tool must never land on the
    triaged upstream."""
    return os.environ.get("PROSPECTOR_FEEDBACK_REPO", "")


@lru_cache(maxsize=1)
def default_branch() -> str:
    """The upstream repository's default branch. TRIAGE_DEFAULT_BRANCH wins when
    set; otherwise discovered once from GitHub via `gh` and cached for the
    process. Falls back to "main" when discovery is unavailable (offline, no gh
    auth)."""
    pinned = os.environ.get("TRIAGE_DEFAULT_BRANCH")
    if pinned:
        return pinned
    from pipeline.gh import operator_env  # deferred: gh.py imports REPO from here
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo()}", "--jq", ".default_branch"],
            capture_output=True, text=True, timeout=15, env=operator_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return "main"
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else "main"

def bot_login() -> str:
    """Login of the GitHub App identity the app executes upstream writes as.
    Empty when no App is configured; a write attributed to nobody is refused."""
    return os.environ.get("TRIAGE_BOT_LOGIN", "")


def store_url() -> str | None:
    """SQLAlchemy URL for the backing store. None → each Store falls back to a
    local SQLite file under its root (dev, CI, OSS-solo). Set it to a shared
    PostgreSQL database (e.g. postgresql+psycopg://…) to point the whole team at
    one store. SQLite and PostgreSQL are the supported store dialects."""
    return os.environ.get("TRIAGE_STORE_URL") or None

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
def verify_scratch() -> Path:
    scratch = os.environ.get("TRIAGE_VERIFY_SCRATCH", "")
    if scratch:
        return Path(scratch).expanduser()
    return Path.home() / ".pr-triage-verify" / f"{repo_owner()}-{repo_name()}"

# Comma-separated host:port entries the sandbox boot probe must FAIL to reach
# (this machine's sensitive host services, e.g. a credentialed local server).
# Empty keeps sandbox/boot-probe.sh's built-in default list.
VERIFY_PROBE_DENY: str = os.environ.get("TRIAGE_VERIFY_PROBE_DENY", "")

# Repository policy profile — path to a JSON file carrying the triaged
# repository's policy vocabulary (pipeline/profile.py owns the schema and
# validation). Relative paths resolve against the repo root. Empty selects the
# built-in generic default profile.
def profile_path() -> str:
    return os.environ.get("TRIAGE_PROFILE", "")


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


def review_provider() -> str:
    """External code-review provider whose verdict gates a clean merge (see
    pipeline/review_policy.py for the profiles). Defaults to "none" so a fresh
    checkout runs without assuming any provider."""
    return parse_review_provider(os.environ.get("TRIAGE_REVIEW_PROVIDER"))


def review_threshold() -> int | None:
    """Override of the review provider's pass threshold. None → the provider's
    built-in bar (greptile → 5)."""
    raw = os.environ.get("TRIAGE_REVIEW_THRESHOLD")
    return int(raw) if raw else None

# The autofix actions a fix request may carry. `update` merges the base branch
# into the PR head, `rebase` rebases onto current base behind a pinned lease,
# `fix` has an agent author a change against a failing gate, and `resolve` has
# an agent resolve merge conflicts inside a merge of the base into the head —
# recorded on requests the worker escalates from a conflicted rebase, never
# queued directly.
FIX_ACTIONS: tuple[str, ...] = ("update", "rebase", "fix", "resolve")

# The machine user that pushes to contributor PR head branches: a GitHub *user*
# account holding push access on REPO, distinct from the App identity in
# BOT_LOGIN. Pushing to a fork head branch rides the PR's "Allow edits from
# maintainers", which grants push to maintainer users — an App installation
# token can never satisfy it. The account authenticates by SSH key alone (no API
# token), so its whole reach is git refs.
#
# All three unset → contributor-branch pushes are unavailable and the app
# surfaces them disabled. Setting only some of them is a misconfiguration, not a
# half-enabled feature: it fails loudly rather than pushing under whatever
# identity happens to be ambient.
PUSH_LOGIN: str = os.environ.get("TRIAGE_PUSH_LOGIN", "").strip()
PUSH_EMAIL: str = os.environ.get("TRIAGE_PUSH_EMAIL", "").strip()
_push_key = os.environ.get("TRIAGE_PUSH_SSH_KEY_FILE", "").strip()
PUSH_SSH_KEY_FILE: Path | None = Path(_push_key).expanduser() if _push_key else None

if any((PUSH_LOGIN, PUSH_EMAIL, PUSH_SSH_KEY_FILE)) and not all(
        (PUSH_LOGIN, PUSH_EMAIL, PUSH_SSH_KEY_FILE)):
    raise SystemExit(
        "TRIAGE_PUSH_LOGIN, TRIAGE_PUSH_EMAIL and TRIAGE_PUSH_SSH_KEY_FILE must be "
        "set together (the machine user's login, its commit email, and its private "
        "key). Set all three or none — see .env.example."
    )


def push_identity_configured() -> bool:
    """Whether this machine holds a complete contributor-push identity. False
    leaves every autofix action unavailable rather than falling back to another
    identity."""
    return bool(PUSH_LOGIN and PUSH_EMAIL and PUSH_SSH_KEY_FILE)


def parse_fix_autopush(raw: str | None) -> frozenset[str]:
    """The autofix actions that push on a clean preflight instead of parking for
    human review. Empty by default; an unknown action name is a hard error so a
    typo never silently withholds the automation it was meant to enable."""
    names = {part.strip().lower() for part in (raw or "").split(",") if part.strip()}
    unknown = sorted(names - set(FIX_ACTIONS))
    if unknown:
        raise SystemExit(
            f"TRIAGE_FIX_AUTOPUSH names unknown actions: {', '.join(unknown)}. "
            f"Valid actions are {', '.join(FIX_ACTIONS)} — see .env.example."
        )
    return frozenset(names)


# The autofix worker's three switches, read live rather than frozen at import:
# the app writes them to .env and reloads, and a toggle that only took effect on
# the next process start would not be the control it presents itself as.


def fix_worker_enabled() -> bool:
    """Whether this machine drains the autofix queue."""
    return os.environ.get("TRIAGE_FIX_WORKER", "") == "1"


def fix_autopush() -> frozenset[str]:
    """The actions that skip `awaiting-review` once their preflight is clean."""
    return parse_fix_autopush(os.environ.get("TRIAGE_FIX_AUTOPUSH"))


def fix_autohunt() -> bool:
    """Whether an idle fix worker queues eligible PRs itself."""
    return os.environ.get("TRIAGE_FIX_AUTOHUNT", "") == "1"


def fix_hunt_fix() -> bool:
    """Whether an idle fix worker may queue agent-authored `fix` actions on its
    own. A separate opt-in from fix_autohunt: mechanical hunting and unattended
    code authoring are different amounts of trust."""
    return os.environ.get("TRIAGE_FIX_HUNT_FIX", "") == "1"


def fix_hunt_limit() -> int:
    """The most auto-queued `fix` requests allowed in flight at once. Each fix
    spends two agents plus a compile preflight, so the hunter feeds them in
    small batches; an unparseable or non-positive value reads as the default."""
    raw = os.environ.get("TRIAGE_FIX_HUNT_LIMIT", "")
    try:
        n = int(raw)
    except ValueError:
        return 3
    return n if n > 0 else 3


# Reject a malformed TRIAGE_FIX_AUTOPUSH while the process is still starting.
# parse_fix_autopush exits on an unknown action, and that belongs at boot rather
# than at whichever read happens to reach it first.
fix_autopush()
