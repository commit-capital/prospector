"""Batched GitHub facts for current PR heads.

One GraphQL request covers up to 40 PRs and returns the inexpensive signals that
share the PR head as their freshness boundary: state, mergeability, CI, aggregate
diffstat, and the first page of changed paths for test-presence classification.
"""
from __future__ import annotations

import logging

from pipeline import diffpaths
from pipeline.gh import gh_graphql
from pipeline.settings import REPO_NAME, REPO_OWNER

_log = logging.getLogger(__name__)

CHUNK_SIZE = 40

_CI_NORM = {"SUCCESS": "passing", "FAILURE": "failing", "ERROR": "failing",
            "PENDING": "pending", "EXPECTED": "pending"}


def _query(prs: list[int]) -> str:
    fields = ("number state merged headRefOid mergeable "
              "additions deletions changedFiles "
              "files(first: 100) { nodes { path } } "
              "commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }")
    aliases = " ".join(f"p{i}: pullRequest(number: {int(n)}) {{ {fields} }}"
                       for i, n in enumerate(prs))
    return f'query {{ repository(owner: "{REPO_OWNER}", name: "{REPO_NAME}") {{ {aliases} }} }}'


def _diffstat(node: dict) -> dict | None:
    values = (node.get("additions"), node.get("deletions"), node.get("changedFiles"))
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return None
    return {"additions": values[0], "deletions": values[1], "changed_files": values[2]}


def fetch(prs: list[int]) -> dict[int, dict]:
    """Fetch live head-bound facts for ``prs``; omit entries GitHub did not return.

    Callers compare the returned keys with their requested IDs. A partial result
    is useful—the facts that arrived can be persisted while missing IDs remain
    eligible for a retry.
    """
    out: dict[int, dict] = {}
    for i in range(0, len(prs), CHUNK_SIZE):
        chunk = prs[i:i + CHUNK_SIZE]
        payload = gh_graphql(_query(chunk))
        if payload is None:
            _log.warning("live PR fetch failed for %d PRs (%d-%d)",
                         len(chunk), chunk[0], chunk[-1])
            continue
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
            rollup = (commits[0] or {}).get("commit", {}).get("statusCheckRollup") or {}
            file_nodes = ((node.get("files") or {}).get("nodes")) or []
            paths = [f.get("path") for f in file_nodes if f.get("path")]
            out[int(n)] = {
                "state": (node.get("state") or "").lower(),
                "merged": bool(node.get("merged")),
                "head": node.get("headRefOid"),
                "mergeable": node.get("mergeable"),
                "ci": _CI_NORM.get(rollup.get("state") or ""),
                "diffstat": diffstat,
                "has_tests": diffpaths.has_tests(paths),
            }
    return out
