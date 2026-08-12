"""Shared config and the bot-token-authenticated read helper for alert_triage.

Alert reads hit private repository-security endpoints (code scanning /
Dependabot / secret scanning), so they authenticate as the configured GitHub
App — gh runs with GH_TOKEN set to a minted installation token — rather than
the operator login. `-X GET` is the hard read-only guarantee: `gh api` with
`-f` fields defaults to POST, and forcing GET sends params as a query string.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pipeline.settings import REPO as REPO
from pipeline.settings import REPO_NAME as REPO_NAME
from pipeline.settings import REPO_OWNER as REPO_OWNER
from pipeline.settings import REPO_ROOT

ROOT = Path(__file__).resolve().parent
GET_TOKEN = REPO_ROOT / "pipeline" / "get-bot-token.sh"


class SourceUnavailable(RuntimeError):
    """The endpoint answered 403/404: the alert feature is disabled on the
    repository or the App installation lacks the permission. Carries which
    source failed so a multi-source sweep can skip it and continue."""

    def __init__(self, source: str, detail: str):
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail


def mint_token() -> str | None:
    """Mint a 1-hour bot installation token via pipeline/get-bot-token.sh, or
    None when minting is unavailable (missing script, key, or configuration).
    The helper reads the PEM only inside its short-lived subprocess."""
    if not GET_TOKEN.exists():
        return None
    try:
        r = subprocess.run(["bash", str(GET_TOKEN)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    tok = (r.stdout or "").strip()
    return tok or None


def _token_env(token: str) -> dict[str, str]:
    env = {**os.environ, "GH_TOKEN": token, "GH_HOST": "github.com"}
    env.pop("GH_CONFIG_DIR", None)
    return env


def _run_gh_read(cmd: list[str], token: str, source: str) -> str:
    """Run one read-only gh invocation, returning stdout. Raises
    SourceUnavailable on an HTTP 403/404 (feature disabled or permission
    missing) and CalledProcessError on any other failure."""
    r = subprocess.run(cmd, capture_output=True, text=True, env=_token_env(token))
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "HTTP 404" in err or "HTTP 403" in err:
            raise SourceUnavailable(source, err.splitlines()[0] if err else "unavailable")
        raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
    return r.stdout


def gh_alert_read(path: str, token: str, params: dict[str, str] | None = None,
                  *, source: str = "") -> object:
    """Run a read-only `gh api -X GET` as the bot and return the parsed JSON."""
    cmd = ["gh", "api", "-X", "GET", path]
    for k, v in (params or {}).items():
        cmd += ["-f", f"{k}={v}"]
    return json.loads(_run_gh_read(cmd, token, source or path))


def gh_alert_read_all(path: str, token: str, params: dict[str, str] | None = None,
                      *, source: str = "") -> list[dict]:
    """Fetch every page of a list endpoint as the bot and return the
    concatenated rows. Pagination is gh's own Link-header following
    (`--paginate`, with `--slurp` collecting the pages into one JSON array of
    page-arrays) — the one mechanism that works across all three alert
    sources, since Dependabot's endpoint paginates by cursor and rejects a
    `page` number."""
    cmd = ["gh", "api", "-X", "GET", "--paginate", "--slurp", path]
    for k, v in (params or {}).items():
        cmd += ["-f", f"{k}={v}"]
    pages = json.loads(_run_gh_read(cmd, token, source or path))
    return [row for page in pages for row in page]
