"""Thin transport over `gh api`: run the CLI, parse its JSON, return None on any
failure so callers degrade gracefully. Domain logic (CI verdicts, reviewer
parsing) lives in the callers; this module only fetches and parses."""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote
from pipeline import settings

_log = logging.getLogger(__name__)

SECONDARY_RATE_LIMIT = "secondary rate limit"
# GitHub's guidance when a secondary limit names no retry-after: wait at least
# a minute, longer on each repeat.
RATE_LIMIT_BACKOFF: tuple[float, ...] = (60, 120, 240)


def operator_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for GitHub reads authenticated by the local ``gh`` login.

    Strips GH_TOKEN/GITHUB_TOKEN so `gh` falls back to the operator's keyring
    login instead of a possibly-stale token left over in the parent process's
    environment. Skipped on a GitHub Actions runner (GITHUB_ACTIONS=true) —
    there is no keyring login to fall back to there, and GH_TOKEN is the
    sanctioned, freshly-injected read identity for that job, not a stale one."""
    env = dict(os.environ if base is None else base)
    if env.get("GITHUB_ACTIONS") != "true":
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
    return env


def gh_api(path: str, *, timeout: int = 60, paginate: bool = False) -> Any | None:
    """`gh api <path>` parsed as JSON, or None on any failure (non-zero exit,
    timeout, unparseable body).

    ``paginate`` follows every page (`--paginate --slurp`) and, when each page
    is an array, returns their items as one flat list. A path gh would parse
    as a flag is refused."""
    if path.startswith("-"):
        return None
    argv = ["gh", "api", path]
    if paginate:
        argv += ["--paginate", "--slurp"]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                             env=operator_env())
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    try:
        parsed = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if paginate and isinstance(parsed, list) and all(isinstance(p, list) for p in parsed):
        return [item for page in parsed for item in page]
    return parsed


def _graphql_once(query: str, variables: Mapping[str, str],
                  timeout: int) -> tuple[dict | None, str]:
    """One `gh api graphql` call: the parsed envelope, or None with the reason.

    Variables go through `-f` (raw string), never `-F` (typed): `-F` coerces an
    all-digit cursor to an int against a query's String variable."""
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        argv += ["-f", f"{k}={v}"]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                             env=operator_env())
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"gh did not run: {exc}"
    reason = (res.stderr or "").strip().removeprefix("gh: ")[:500] or f"exit {res.returncode}"
    try:
        parsed = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None, reason
    if not isinstance(parsed, dict):
        return None, reason
    if res.returncode != 0 and not isinstance(parsed.get("data"), dict):
        return None, reason
    return parsed, ""


def _graphql(query: str, variables: Mapping[str, str] | None, timeout: int,
             rate_limit_waits: Sequence[float]) -> tuple[dict | None, str]:
    """`_graphql_once`, retrying a GitHub secondary rate limit after each wait in
    ``rate_limit_waits`` (seconds) in turn."""
    parsed, reason = _graphql_once(query, variables or {}, timeout)
    for wait in rate_limit_waits:
        if parsed is not None or SECONDARY_RATE_LIMIT not in reason.lower():
            break
        _log.warning("GitHub secondary rate limit; retrying in %ds", wait)
        time.sleep(wait)
        parsed, reason = _graphql_once(query, variables or {}, timeout)
    return parsed, reason


def gh_graphql(query: str, *, variables: Mapping[str, str] | None = None,
               timeout: int = 60, rate_limit_waits: Sequence[float] = ()) -> dict | None:
    """`gh api graphql` for `query`, parsed as a JSON object, or None when no
    usable response came back (timeout, unparseable/non-object body, or an
    error body with no `data`); a None is logged with gh's own error text.

    gh exits non-zero on any GraphQL error while still printing the full
    envelope, and errors coexist with partial data — so a body carrying a
    `data` object is returned regardless of exit code.

    A caller answering an HTTP request passes no ``rate_limit_waits`` and fails
    fast."""
    parsed, reason = _graphql(query, variables, timeout, rate_limit_waits)
    if parsed is None:
        _log.warning("gh api graphql failed: %s", reason)
    return parsed


def gh_graphql_data(query: str, *, variables: Mapping[str, str] | None = None,
                    timeout: int = 60, rate_limit_waits: Sequence[float] = ()) -> dict:
    """The `data` object of a `gh api graphql` call that returned no GraphQL
    errors. Anything less raises RuntimeError naming gh's error text or the
    GraphQL errors, for a caller that must not proceed on partial data."""
    parsed, reason = _graphql(query, variables, timeout, rate_limit_waits)
    if parsed is None:
        raise RuntimeError(f"GraphQL query failed: {reason}")
    if parsed.get("errors") or not isinstance(parsed.get("data"), dict):
        raise RuntimeError(f"GraphQL query failed: {parsed.get('errors')}")
    return parsed["data"]


def gh_json(path: str, *, timeout: int = 60) -> dict | None:
    """`gh api <path>` parsed as a JSON object, or None on any failure or a
    non-object body."""
    parsed = gh_api(path, timeout=timeout)
    return parsed if isinstance(parsed, dict) else None


def gh_list(path: str, *, timeout: int = 60, paginate: bool = False) -> list[dict] | None:
    """`gh api <path>` parsed as a JSON array, or None on any failure or a
    non-array body. ``paginate`` reads every page into the one list."""
    parsed = gh_api(path, timeout=timeout, paginate=paginate)
    return parsed if isinstance(parsed, list) else None


def default_branch_file(path: str, *, timeout: int = 60) -> str | None:
    """The text of the file at repo-relative `path` on the repository's default
    branch, or None when it has none or GitHub did not answer."""
    doc = gh_json(f"repos/{settings.repo()}/contents/{quote(path)}", timeout=timeout)
    raw = (doc or {}).get("content")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def fetch_pr(n: int, *, timeout: int = 60) -> dict | None:
    """One PR's raw gh object (`repos/<repo>/pulls/{n}`), or None if unreachable."""
    return gh_json(f"repos/{settings.repo()}/pulls/{int(n)}", timeout=timeout)


def issue_comments(n: int, *, timeout: int = 60) -> list[dict] | None:
    """Every comment on issue or PR `n`, across all pages, or None when the
    listing is unavailable."""
    return gh_list(f"repos/{settings.repo()}/issues/{int(n)}/comments?per_page=100",
                   timeout=timeout, paginate=True)


def pr_reviews(n: int, *, timeout: int = 60) -> list[dict] | None:
    """Every review on PR `n`, oldest first across all pages, or None when the
    listing is unavailable."""
    return gh_list(f"repos/{settings.repo()}/pulls/{int(n)}/reviews?per_page=100",
                   timeout=timeout, paginate=True)


def pr_files(n: int, *, timeout: int = 120) -> list[dict] | None:
    """PR `n`'s per-file listing (filename, status, counts, patch), across all
    pages, or None when the listing is unavailable."""
    return gh_list(f"repos/{settings.repo()}/pulls/{int(n)}/files?per_page=100",
                   timeout=timeout, paginate=True)


def pr_changed_paths(n: int, *, timeout: int = 120) -> list[str] | None:
    """Every path PR `n` changes, from its per-file listing, or None when the
    listing is unavailable."""
    files = pr_files(n, timeout=timeout)
    if files is None:
        return None
    return [f["filename"] for f in files if isinstance(f.get("filename"), str)]


def operator_login(*, timeout: int = 20) -> str | None:
    """The login the local `gh` reads as, or None when gh is absent, signed out,
    or unreachable."""
    login = (gh_json("user", timeout=timeout) or {}).get("login")
    return login if isinstance(login, str) and login else None


def check_runs(sha: str) -> list[dict]:
    """The commit's check runs from GitHub as `[{app, name, status, conclusion,
    title, summary, url}]`, deduped by (name, conclusion), superseded re-runs
    dropped via filter=latest. Empty when `sha` is falsy, GitHub has no checks
    for it, or the fetch fails."""
    from pipeline import ci_signal
    if not sha:
        return []
    data = gh_json(f"repos/{settings.repo()}/commits/{sha}/check-runs?per_page=100&filter=latest")
    seen: set[tuple[str | None, str | None]] = set()
    out: list[dict] = []
    for item in ci_signal.from_rest_check_runs((data or {}).get("check_runs", [])):
        key = (item["name"], item["conclusion"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out
