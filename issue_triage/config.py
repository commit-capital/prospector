"""Shared paths and a read-only GitHub helper for the issue_triage pipeline."""
import json
import subprocess
from pathlib import Path

# re-exported for issue_triage modules
from pipeline.settings import REPO as REPO
from pipeline.settings import REPO_NAME as REPO_NAME
from pipeline.settings import REPO_OWNER as REPO_OWNER

ROOT = Path(__file__).resolve().parent


def gh_read(path, params=None):
    """Run a read-only `gh api` GET.

    `-X GET` is REQUIRED: `gh api` with `-f` fields defaults to POST, which would
    turn a read into a write attempt against the upstream repo (forbidden by
    CLAUDE.md). Forcing GET sends params as query string and guarantees no write.
    """
    cmd = ["gh", "api", "-X", "GET", path]
    for k, v in (params or {}).items():
        cmd += ["-f", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def gh_graphql(query: str, variables: dict[str, str] | None = None) -> dict:
    """Run a read-only GraphQL query via `gh api graphql`; return the `data` object.

    GraphQL is POST-only, so the `-X GET` guard that keeps gh_read read-only can't
    apply here — the read-only guarantee instead rests on the document being a
    `query`, never a `mutation` (callers pass query text only). Variables go through
    `-f` (raw string fields), matching the `String`-typed variables the queries
    declare: `-f` never applies `gh`'s literal detection, so a cursor that happens
    to be all-digits stays a string instead of coercing to an int. A first-page
    null cursor is expressed by omitting the variable, never by passing one here.
    """
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in (variables or {}).items():
        cmd += ["-f", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    payload = json.loads(out.stdout)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL query failed: {payload['errors']}")
    return payload["data"]
