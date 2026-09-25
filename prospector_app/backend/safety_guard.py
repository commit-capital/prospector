"""Subprocess guard for the app backend — two enforced layers.

`run()` is the read-only path for the backend's own shell-outs (reads run as the
operator's local `gh` login). It:

  * allows only an explicit set of executables (`gh`, `git`, `claude`, `python*`)
  * rejects any GitHub *write* form (gh pr comment/close/edit/merge/review,
    gh issue create/edit/close, gh api with -X POST/PATCH/DELETE/PUT,
    git push to the upstream remote, curl writes to api.github.com)

Backend reads use the operator's local `gh` login.

Upstream writes go out only through the sanctioned bot paths below —
`bot_run` (executor comments / closes / reopens / reviews), `chat_bot_run`
(validated embedded-agent edits / comments / issue writes / workflow reruns),
`alert_bot_run` (alert dismissals), `propose_bot_run` (an issue-fix pull request
opened from the push user's lane branch), and `bot_merge_run` (squash-merge).
All require a non-empty installation token and
a server checkout compatible with the shared store, then inject the token via
GH_TOKEN for that one subprocess.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import TypedDict

from pipeline import settings
from pipeline import schema
from pipeline import storekit
from pipeline.gh import operator_env

ALLOWED_BINARIES = {"gh", "git", "claude", "python", "python3"}

# What the chat agent's process needs to start, authenticate and push as the
# confirming operator. `SSH_AUTH_SOCK` is how an interactive resubmit reaches
# the operator's own key.
_AGENT_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG",
                   "TERM", "SSH_AUTH_SOCK")
_AGENT_ENV_PREFIXES = ("LC_", "ANTHROPIC_", "CLAUDE_", "CODEX_",
                       "TRIAGE_", "PROSPECTOR_")

# The deployment value the agent's environment withholds. `jq` is an
# allowlisted text filter and `jq -n env` prints the environment, so the store
# URL's password would be one command from any text an outsider wrote. Helpers
# that need the store read it from the repo-root .env, which pipeline.settings
# loads on import; a deployment configured by process environment alone, with
# no .env on disk, loses `store-read` in chat.
_AGENT_ENV_DROP = ("TRIAGE_STORE_URL",)


def agent_env() -> dict[str, str]:
    """The environment for one chat-agent turn: the operator's, held to what
    the CLI and the curated helpers need. Its Bash commands inherit this, so
    what is not here is out of a prompt injection's reach."""
    return {
        key: value for key, value in operator_env().items()
        if key not in _AGENT_ENV_DROP
        and (key in _AGENT_ENV_KEEP or key.startswith(_AGENT_ENV_PREFIXES))
    }


# --- denied patterns, matched against the full argv joined with spaces -------
_DENY = [
    # gh write subcommands
    re.compile(r"\bgh\s+pr\s+(comment|close|edit|merge|review|create|reopen|ready)\b"),
    re.compile(r"\bgh\s+issue\s+(create|edit|close|comment|reopen|delete)\b"),
    re.compile(r"\bgh\s+release\b"),
    # NOTE: do NOT put \b before "-X" — a space-to-hyphen transition is not a
    # word boundary, so \b-X never matches "gh api -X POST". Anchor on whitespace.
    re.compile(r"\bgh\s+api\b.*(?:^|\s)-X\s*(POST|PATCH|DELETE|PUT)\b", re.I),
    re.compile(r"\bgh\s+api\b.*--method[=\s]+(POST|PATCH|DELETE|PUT)\b", re.I),
    # git writes that could reach upstream
    re.compile(r"\bgit\s+push\b"),
    # raw curl writes to the GitHub API
    re.compile(r"\bcurl\b.*(?:^|\s)-X\s*(POST|PATCH|DELETE|PUT)\b", re.I),
    re.compile(r"--request[=\s]+(POST|PATCH|DELETE|PUT)\b", re.I),
    re.compile(r"api\.github\.com.*(?:^|\s)-X\s*(POST|PATCH|DELETE|PUT)\b", re.I),
]


class WriteAttemptBlocked(RuntimeError):
    """Raised when a command cannot run through a sanctioned upstream path."""


class StoreSchemaStatus(TypedDict):
    code_version: int
    store_version: int | None
    write_block: str | None


def store_schema_status() -> StoreSchemaStatus:
    """Whether this checkout can safely write alongside the shared store."""
    from prospector_app.backend import data

    code_version = schema.STORE_SCHEMA_VERSION
    try:
        store_version = storekit.refresh_schema_guard(data.store().engine)
    except Exception:
        return {
            "code_version": code_version,
            "store_version": None,
            "write_block": "Can't verify the shared store schema. Live actions are disabled.",
        }
    write_block = None
    if store_version > code_version:
        write_block = (
            f"This server supports store schema v{code_version}, but the shared store is v{store_version}. "
            "Live actions are disabled until this server checkout is updated."
        )
    return {
        "code_version": code_version,
        "store_version": store_version,
        "write_block": write_block,
    }


def assert_store_writes_safe() -> None:
    """Refuse upstream writes from code older than the shared store."""
    status = store_schema_status()
    if status["write_block"]:
        raise WriteAttemptBlocked(status["write_block"])


def assert_read_only(argv: list[str]) -> None:
    if not argv:
        raise WriteAttemptBlocked("empty command")
    binary = argv[0].rsplit("/", 1)[-1]
    if binary not in ALLOWED_BINARIES:
        raise WriteAttemptBlocked(f"binary not allowed: {binary!r}")
    joined = " ".join(argv)
    for pat in _DENY:
        if pat.search(joined):
            raise WriteAttemptBlocked(f"blocked write-shaped command: {joined!r}")


def run(argv: list[str], *, timeout: int = 120, text: bool = True) -> subprocess.CompletedProcess:
    """Run a read-only command. Raises WriteAttemptBlocked if it looks like a write."""
    assert_read_only(argv)
    return subprocess.run(argv, capture_output=True, text=text, timeout=timeout,
                          env=operator_env())


# ---------------------------------------------------------------------------
# Sanctioned bot write path (M6).
#
# Bot writes use the configured identity, a non-empty installation token, and an
# explicit operation allowlist. The token is scoped to one subprocess.
# ---------------------------------------------------------------------------
BOT_WRITE_ALLOW = [
    re.compile(r"^gh\s+pr\s+comment\s+\d+\b"),
    re.compile(r"^gh\s+pr\s+close\s+\d+\b"),
    re.compile(r"^gh\s+pr\s+reopen\s+\d+\b"),                     # undo a close
    re.compile(r"^gh\s+pr\s+review\s+\d+\b"),                     # approve / request-changes / comment
    re.compile(r"^gh\s+issue\s+comment\s+\d+\b"),                 # comment on an issue (#192)
    re.compile(r"^gh\s+issue\s+close\s+\d+\b"),                   # close an issue as a duplicate
    re.compile(r"^gh\s+issue\s+reopen\s+\d+\b"),                  # undo an issue close
    re.compile(r"^gh\s+api\s+repos/\S+/issues/\d+/comments\b"),   # POST an issue/PR comment
    re.compile(r"^gh\s+api\b.*\bpulls/\d+/comments\b"),           # POST an inline review comment
    # dismiss one of our own PR reviews (undo a request-changes on reopen, #70)
    re.compile(r"^gh\s+api\b.*\bpulls/\d+/reviews/\d+/dismissals\b"),
    # DELETE one of our own comments (undo) — scoped to issues/comments/<id>
    re.compile(r"^gh\s+api\s+(?:-X\s*DELETE|--method[=\s]+DELETE)\s+repos/\S+/issues/comments/\d+\b"),
    re.compile(r"^gh\s+api\s+repos/\S+/issues/comments/\d+\s+(?:-X\s*DELETE|--method[=\s]+DELETE)\b"),
]
# Merge uses bot_merge_run and its executor store gate. The ordinary bot-write
# path is scoped to comments, closes, reopens, and reviews.
_BOT_FORBID = re.compile(r"\bgh\s+pr\s+(merge|edit)\b")

CHAT_BOT_WRITE_ALLOW = [
    re.compile(r"^gh\s+pr\s+comment\s+\d+\b"),
    re.compile(r"^gh\s+pr\s+edit\s+\d+\b"),
    re.compile(r"^gh\s+issue\s+comment\s+\d+\b"),
    re.compile(r"^gh\s+issue\s+create\b"),
    re.compile(r"^gh\s+issue\s+edit\s+\d+\b"),
    re.compile(r"^gh\s+issue\s+reopen\s+\d+\b"),
    re.compile(r"^gh\s+run\s+rerun\s+\d+\b"),
]


def assert_bot_write(argv: list[str]) -> None:
    if not argv or argv[0].rsplit("/", 1)[-1] != "gh":
        raise WriteAttemptBlocked("bot writes must use gh")
    joined = " ".join(argv)
    if _BOT_FORBID.search(joined):
        raise WriteAttemptBlocked(f"operation not allowed via the bot-write path: {joined!r}")
    if not any(p.search(joined) for p in BOT_WRITE_ALLOW):
        raise WriteAttemptBlocked(f"not an allowlisted bot write: {joined!r}")


def assert_chat_bot_write(argv: list[str]) -> None:
    if not argv or argv[0].rsplit("/", 1)[-1] != "gh":
        raise WriteAttemptBlocked("chat bot writes must use gh")
    joined = " ".join(argv)
    if re.search(r"\bgh\s+pr\s+merge\b", joined):
        raise WriteAttemptBlocked(f"operation not allowed via the chat bot-write path: {joined!r}")
    if not any(p.search(joined) for p in CHAT_BOT_WRITE_ALLOW):
        raise WriteAttemptBlocked(f"not an allowlisted chat bot write: {joined!r}")


ALERT_WRITE_ALLOW = [
    re.compile(r"^gh\s+api\s+(?:-X\s*PATCH|--method[=\s]+PATCH)\s+"
               r"repos/\S+/(?:code-scanning|dependabot|secret-scanning)/alerts/\d+\b"),
]


def assert_alert_bot_write(argv: list[str]) -> None:
    if not argv or argv[0].rsplit("/", 1)[-1] != "gh":
        raise WriteAttemptBlocked("alert writes must use gh")
    joined = " ".join(argv)
    if not any(p.search(joined) for p in ALERT_WRITE_ALLOW):
        raise WriteAttemptBlocked(f"not an allowlisted alert write: {joined!r}")


PROPOSE_KEYS = frozenset({"title", "body", "head", "base", "maintainer_can_modify"})
_PROPOSE_HEAD_RE = re.compile(r"^([A-Za-z0-9-]+):prospector/issue-[1-9][0-9]{0,8}-[0-9a-f]{8}$")


def assert_propose_write(payload: dict) -> None:
    """Hold a proposed pull request to the one shape the issue-fix lane opens:
    from the push user's lane branch into TRIAGE_REPO's default branch, with
    nothing but a title, a body and maintainer edits allowed."""
    if set(payload) != PROPOSE_KEYS:
        raise WriteAttemptBlocked(f"a proposal carries exactly {sorted(PROPOSE_KEYS)}")
    m = _PROPOSE_HEAD_RE.fullmatch(str(payload["head"]))
    if not m or not settings.push_login() or m.group(1) != settings.push_login():
        raise WriteAttemptBlocked(f"{payload['head']!r} is not the push user's lane branch")
    if payload["base"] != settings.default_branch():
        raise WriteAttemptBlocked(f"a proposal targets {settings.default_branch()!r}")
    if payload["maintainer_can_modify"] is not True:
        raise WriteAttemptBlocked("a proposal allows maintainer edits")
    if not isinstance(payload["title"], str) or not isinstance(payload["body"], str):
        raise WriteAttemptBlocked("a proposal's title and body are text")


def propose_bot_run(payload: dict, token: str, *,
                    timeout: int = 60) -> subprocess.CompletedProcess:
    """Open a pull request on TRIAGE_REPO as the configured bot."""
    token = _require_bot_token(token, "open a pull request")
    assert_propose_write(payload)
    assert_store_writes_safe()
    argv = ["gh", "api", "--method", "POST", f"repos/{settings.repo()}/pulls", "--input", "-"]
    return subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                          timeout=timeout, env=bot_env(token))


_MERGE_RE = re.compile(r"^gh\s+pr\s+merge\s+\d+\b")


def _require_bot_identity(action: str) -> str:
    """The configured bot login. A deployment set up without a GitHub App has
    none, and a write attributed to nobody is refused rather than attempted."""
    login = settings.bot_login()
    if not login:
        raise WriteAttemptBlocked(
            f"refusing to {action}: no bot identity is configured")
    return login


def _require_bot_token(token: str | None, action: str) -> str:
    login = _require_bot_identity(action)
    if not token or not token.strip():
        raise WriteAttemptBlocked(
            f"refusing to {action} without a {login} token (would fall back to default login)"
        )
    return token


def bot_run(argv: list[str], token: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a sanctioned bot write as the configured bot via GH_TOKEN."""
    token = _require_bot_token(token, "write")
    assert_bot_write(argv)
    assert_store_writes_safe()
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=bot_env(token))


def chat_bot_run(argv: list[str], token: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a validated embedded-agent write with a non-empty bot token."""
    token = _require_bot_token(token, "write")
    assert_chat_bot_write(argv)
    assert_store_writes_safe()
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=bot_env(token))


def alert_bot_run(argv: list[str], token: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a sanctioned alert dismissal/resolution as the configured bot."""
    token = _require_bot_token(token, "dismiss an alert")
    assert_alert_bot_write(argv)
    assert_store_writes_safe()
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=bot_env(token))


def bot_merge_run(argv: list[str], token: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a gated upstream squash-merge as the configured bot.

    The command shape is limited to ``gh pr merge <n>``.
    """
    token = _require_bot_token(token, "merge")
    if not argv or argv[0].rsplit("/", 1)[-1] != "gh":
        raise WriteAttemptBlocked("merge must use gh")
    if not _MERGE_RE.match(" ".join(argv)):
        raise WriteAttemptBlocked(f"not a pr-merge command: {' '.join(argv)!r}")
    assert_store_writes_safe()
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=bot_env(token))


def bot_env(token: str) -> dict[str, str]:
    """Build the configured bot environment for a GitHub subprocess."""
    env = {**os.environ, "GH_TOKEN": token, "GH_HOST": "github.com"}
    env.pop("GH_CONFIG_DIR", None)
    return env
