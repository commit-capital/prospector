"""Batched GitHub facts for current PR heads.

One GraphQL request covers up to 40 PRs and returns the inexpensive signals that
share the PR head as their freshness boundary: state, mergeability, the head's
check runs and statuses (CI, with reviewer-owned runs carried separately for the
reviewer adapters), aggregate diffstat, the first page of changed paths for
test-presence classification, and the PR's updatedAt for the incremental feed.
"""
from __future__ import annotations

import logging

from pipeline import ci_signal
from pipeline import diffpaths
from pipeline import settings
from pipeline.gh import gh_graphql

_log = logging.getLogger(__name__)

CHUNK_SIZE = 40


def _query(prs: list[int]) -> str:
    fields = ("number state merged headRefOid mergeable updatedAt "
              "additions deletions changedFiles "
              "files(first: 100) { nodes { path } } "
              "commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { "
              "__typename ... on CheckRun { name status conclusion title summary detailsUrl url "
              "checkSuite { app { slug } } } ... on StatusContext { context state } } } } } } }")
    aliases = " ".join(f"p{i}: pullRequest(number: {int(n)}) {{ {fields} }}"
                       for i, n in enumerate(prs))
    return f'query {{ repository(owner: "{settings.repo_owner()}", name: "{settings.repo_name()}") {{ {aliases} }} }}'


def _diffstat(node: dict) -> dict | None:
    values = (node.get("additions"), node.get("deletions"), node.get("changedFiles"))
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return None
    return {"additions": values[0], "deletions": values[1], "changed_files": values[2]}


def _not_found_numbers(errors: list, chunk: list[int]) -> set[int]:
    """The PR numbers in ``chunk`` whose alias GitHub reported NOT_FOUND. An
    error's path names the failing alias (["repository", "pN"]), and the alias
    index maps back to the requested number. Only an explicit NOT_FOUND type
    with a per-alias path qualifies — a repository-level error or a transient
    type (rate limit, server error) marks nothing."""
    numbers: set[int] = set()
    for err in errors:
        err = err or {}
        if err.get("type") != "NOT_FOUND":
            continue
        path = err.get("path")
        if (isinstance(path, list) and len(path) == 2 and path[0] == "repository"
                and isinstance(path[1], str) and path[1][:1] == "p"
                and path[1][1:].isdigit()):
            idx = int(path[1][1:])
            if idx < len(chunk):
                numbers.add(int(chunk[idx]))
    return numbers


def fetch(prs: list[int]) -> tuple[dict[int, dict], set[int]]:
    """Fetch live head-bound facts for ``prs``.

    Returns ``(facts, not_found)``: facts keyed by PR number, plus the requested
    numbers GitHub explicitly reported NOT_FOUND — the PR no longer exists
    upstream (scrubbed/deleted), a deterministic verdict callers may act on.
    A number in neither is a transient miss (failed request, other GraphQL
    error) and remains eligible for a retry.
    """
    out: dict[int, dict] = {}
    not_found: set[int] = set()
    for i in range(0, len(prs), CHUNK_SIZE):
        chunk = prs[i:i + CHUNK_SIZE]
        payload = gh_graphql(_query(chunk))
        if payload is None:
            _log.warning("live PR fetch failed for %d PRs (%d-%d)",
                         len(chunk), chunk[0], chunk[-1])
            continue
        errors = payload.get("errors")
        if errors:
            # Partial envelope: the erroring aliases come back as null nodes
            # and are omitted from the facts; every resolved alias in the
            # batch is still used, and NOT_FOUND aliases are reported.
            not_found |= _not_found_numbers(errors, chunk)
            _log.warning("live PR fetch: %d GraphQL error(s) in batch %d-%d; first: %s",
                         len(errors), chunk[0], chunk[-1],
                         (errors[0] or {}).get("message", "?"))
        repo = (payload.get("data") or {}).get("repository") or {}
        for j, n in enumerate(chunk):
            node = repo.get(f"p{j}")
            if not isinstance(node, dict):
                continue
            diffstat = _diffstat(node)
            if diffstat is None:
                _log.warning("live PR fetch returned incomplete diffstat for PR #%d", n)
                continue
            commits = ((node.get("commits") or {}).get("nodes")) or [{}]
            rollup = ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") or {}
            runs, statuses = ci_signal.from_graphql_contexts(
                (rollup.get("contexts") or {}).get("nodes") or [])
            file_nodes = ((node.get("files") or {}).get("nodes")) or []
            paths = [f.get("path") for f in file_nodes if f.get("path")]
            out[int(n)] = {
                "state": (node.get("state") or "").lower(),
                "merged": bool(node.get("merged")),
                "head": node.get("headRefOid"),
                "mergeable": node.get("mergeable"),
                "updated_at": node.get("updatedAt"),
                "ci": ci_signal.verdict(runs, statuses),
                "check_runs": runs,
                "statuses": statuses,
                "diffstat": diffstat,
                "has_tests": diffpaths.has_tests(paths),
            }
    return out, not_found
