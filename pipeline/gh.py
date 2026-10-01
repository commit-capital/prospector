"""Thin transport over `gh api`: run the CLI, parse its JSON, return None on any
failure so callers degrade gracefully. Domain logic (CI verdicts, reviewer
parsing) lives in the callers; this module only fetches and parses."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from typing import Any
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


def gh_api(path: str, *, timeout: int = 60) -> Any | None:
    """`gh api <path>` parsed as JSON, or None on any failure (non-zero exit,
    timeout, unparseable body)."""
    try:
        res = subprocess.run(["gh", "api", path],
                             capture_output=True, text=True, timeout=timeout,
                             env=operator_env())
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def _graphql_once(query: str, timeout: int) -> tuple[dict | None, str]:
    """One `gh api graphql` call: the parsed envelope, or None with the reason."""
    try:
        res = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                             capture_output=True, text=True, timeout=timeout,
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


def gh_graphql(query: str, *, timeout: int = 60,
               rate_limit_waits: Sequence[float] = ()) -> dict | None:
    """`gh api graphql` for `query`, parsed as a JSON object, or None when no
    usable response came back (timeout, unparseable/non-object body, or an
    error body with no `data`); a None is logged with gh's own error text.

    gh exits non-zero on any GraphQL error while still printing the full
    envelope, and errors coexist with partial data — so a body carrying a
    `data` object is returned regardless of exit code.

    A GitHub secondary rate limit is retried after each wait in
    ``rate_limit_waits`` (seconds) in turn. A caller answering an HTTP request
    passes none and fails fast."""
    parsed, reason = _graphql_once(query, timeout)
    for wait in rate_limit_waits:
        if parsed is not None or SECONDARY_RATE_LIMIT not in reason.lower():
            break
        _log.warning("GitHub secondary rate limit; retrying in %ds", wait)
        time.sleep(wait)
        parsed, reason = _graphql_once(query, timeout)
    if parsed is None:
        _log.warning("gh api graphql failed: %s", reason)
    return parsed


def gh_json(path: str) -> dict | None:
    """`gh api <path>` parsed as a JSON object, or None on any failure or a
    non-object body."""
    parsed = gh_api(path)
    return parsed if isinstance(parsed, dict) else None


def gh_list(path: str) -> list[dict] | None:
    """`gh api <path>` parsed as a JSON array, or None on any failure or a
    non-array body."""
    parsed = gh_api(path)
    return parsed if isinstance(parsed, list) else None


def fetch_pr(n: int) -> dict | None:
    """One PR's raw gh object (`repos/<repo>/pulls/{n}`), or None if unreachable."""
    return gh_json(f"repos/{settings.repo()}/pulls/{int(n)}")


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
