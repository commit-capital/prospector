"""Subprocess guard for the cockpit backend — two enforced layers.

`run()` is the read-only path for the backend's own shell-outs (reads run as the
operator's local `gh` login). It:

  * allows only an explicit set of executables (`gh`, `git`, `claude`, `python*`)
  * rejects any GitHub *write* form (gh pr comment/close/edit/merge/review,
    gh issue create/edit/close, gh api with -X POST/PATCH/DELETE/PUT,
    git push to the upstream remote, curl writes to api.github.com)

so the cockpit can never write to the upstream repo as the operator's local
`gh` login.

Upstream writes go out only through the sanctioned bot paths below —
`bot_run` (comment / close / reopen / review / inline comment) and
`bot_merge_run` (squash-merge). Both REQUIRE a non-empty installation token (an
empty token would fall back to the default login, so we refuse) and inject it
via GH_TOKEN for that one subprocess. On a machine where no bot token
can be minted, the executor obtains no token and these paths are unreachable —
the cockpit stays effectively read-only.
"""
from __future__ import annotations

import os
import re
import subprocess

from pipeline.settings import BOT_LOGIN

ALLOWED_BINARIES = {"gh", "git", "claude", "python", "python3"}

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
    """Raised when a command looks like an upstream write."""


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
    return subprocess.run(argv, capture_output=True, text=text, timeout=timeout)


# ---------------------------------------------------------------------------
# Sanctioned bot write path (M6).
#
# The cockpit may post to the upstream repo ONLY as the configured bot,
# ONLY for close + comment actions, and ONLY with a real installation token.
# This mirrors /resolve-pr-cluster. The hard guarantees:
#   * a non-empty token is REQUIRED — an empty token would make `gh` fall back
#     to the operator's default `gh` login, which is forbidden, so we refuse.
#   * the token is injected via GH_TOKEN for that one subprocess only.
#   * only an allowlist of write ops is permitted; merge is never allowed.
# On a machine with no readable configured bot key the executor obtains no token, so
# this path is unreachable and the cockpit stays effectively read-only.
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
# Merge has its OWN sanctioned path (bot_merge_run) gated by the executor's
# store-check — it is deliberately NOT in the comment/close allowlist, so the
# ordinary bot-write path can never merge. Edits are never allowed at all.
_BOT_FORBID = re.compile(r"\bgh\s+pr\s+(merge|edit)\b")


def assert_bot_write(argv: list[str]) -> None:
    if not argv or argv[0].rsplit("/", 1)[-1] != "gh":
        raise WriteAttemptBlocked("bot writes must use gh")
    joined = " ".join(argv)
    if _BOT_FORBID.search(joined):
        raise WriteAttemptBlocked(f"operation not allowed via the bot-write path: {joined!r}")
    if not any(p.search(joined) for p in BOT_WRITE_ALLOW):
        raise WriteAttemptBlocked(f"not an allowlisted bot write: {joined!r}")


_MERGE_RE = re.compile(r"^gh\s+pr\s+merge\s+\d+\b")


def bot_run(argv: list[str], token: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a sanctioned bot write (comment / close / reopen / review /
    inline comment). Requires a non-empty token; the write goes out as
    the configured bot via GH_TOKEN, never the default login."""
    if not token or not token.strip():
        raise WriteAttemptBlocked(f"refusing to write without a {BOT_LOGIN} token (would fall back to default login)")
    assert_bot_write(argv)
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=bot_env(token))


def bot_merge_run(argv: list[str], token: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Sanctioned UPSTREAM MERGE as the configured bot — the one write that
    is NOT a comment/close. Requires a non-empty token and matches only `gh pr
    merge <n>`. With no configured bot key the executor never mints a token, so
    this path is unreachable and the cockpit cannot merge."""
    if not token or not token.strip():
        raise WriteAttemptBlocked(f"refusing to merge without a {BOT_LOGIN} token (would fall back to default login)")
    if not argv or argv[0].rsplit("/", 1)[-1] != "gh":
        raise WriteAttemptBlocked("merge must use gh")
    if not _MERGE_RE.match(" ".join(argv)):
        raise WriteAttemptBlocked(f"not a pr-merge command: {' '.join(argv)!r}")
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=bot_env(token))


def bot_env(token: str) -> dict[str, str]:
    """Subprocess env that authenticates `gh`/`git` as the configured bot: inject the
    minted `GH_TOKEN`, pin `GH_HOST`, and drop `GH_CONFIG_DIR` so the local gh
    config is never consulted (no fall-back to the operator's login)."""
    env = {**os.environ, "GH_TOKEN": token, "GH_HOST": "github.com"}
    env.pop("GH_CONFIG_DIR", None)
    return env
