"""Thin transport over `gh api`: run the CLI, parse its JSON, return None on any
failure so callers degrade gracefully. Domain logic (CI verdicts, Greptile
parsing) lives in the callers; this module only fetches and parses."""
from __future__ import annotations

import json
import subprocess
from typing import Any

from pipeline.settings import REPO


def gh_api(path: str, *, timeout: int = 60) -> Any | None:
    """`gh api <path>` parsed as JSON, or None on any failure (non-zero exit,
    timeout, unparseable body)."""
    try:
        res = subprocess.run(["gh", "api", path],
                             capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def gh_graphql(query: str, *, timeout: int = 60) -> dict | None:
    """`gh api graphql` for `query`, parsed as a JSON object, or None when no
    usable response came back (timeout, unparseable/non-object body, or an
    error body with no `data`).

    gh exits non-zero on any GraphQL error while still printing the full
    envelope, and errors coexist with partial data — so a body carrying a
    `data` object is returned regardless of exit code."""
    try:
        res = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                             capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        parsed = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if res.returncode != 0 and not isinstance(parsed.get("data"), dict):
        return None
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
    """One PR's raw gh object (`repos/REPO/pulls/{n}`), or None if unreachable."""
    return gh_json(f"repos/{REPO}/pulls/{int(n)}")


def check_runs(sha: str) -> list[dict]:
    """The commit's CI check runs from GitHub as `[{name, conclusion, status}]`,
    deduped by (name, conclusion), superseded re-runs dropped via filter=latest.
    Empty when `sha` is falsy, GitHub has no checks for it, or the fetch fails."""
    if not sha:
        return []
    data = gh_json(f"repos/{REPO}/commits/{sha}/check-runs?per_page=100&filter=latest")
    seen: set[tuple[str | None, str | None]] = set()
    out: list[dict] = []
    for r in (data or {}).get("check_runs", []):
        item = {"name": r.get("name"), "conclusion": r.get("conclusion"),
                "status": r.get("status")}
        key = (item["name"], item["conclusion"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out
