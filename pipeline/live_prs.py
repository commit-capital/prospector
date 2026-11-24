"""Batched GitHub facts for current PR heads.

The full read covers CHUNK_SIZE PRs per GraphQL request and returns the signals
that share the PR head as their freshness boundary: state, mergeability, the
head's check runs and statuses (CI, with reviewer-owned runs carried separately
for the reviewer adapters), aggregate diffstat, the first page of changed paths
for test-presence classification, the PR's updatedAt for the incremental feed,
and whether maintainers may push to its head branch.

A caller holding stored PRs first probes them PROBE_CHUNK_SIZE at a time for
the state alone plus the check basis (`ci_signal.basis`). A PR the probe
confirms (`freshness.ci_confirmed`) keeps its stored CI, diffstat and test
presence; the rest get the full read.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pipeline import ci_signal
from pipeline import diffpaths
from pipeline import freshness
from pipeline import progress
from pipeline import settings
from pipeline.gh import RateLimited, complete_pr_checks, gh_graphql, required_checks

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pipeline.model import Pr

_log = logging.getLogger(__name__)

CHUNK_SIZE = 20
RETRY_CHUNK_SIZE = 10
PROBE_CHUNK_SIZE = 100

_STATE_FIELDS = "number state merged headRefOid mergeable updatedAt maintainerCanModify "
_PROBE_FIELDS = _STATE_FIELDS + ci_signal.BASIS_FIELDS
_FIELDS = (_STATE_FIELDS + "additions deletions changedFiles "
           "files(first: 100) { nodes { path } } " + ci_signal.PR_FIELDS)


def _query(prs: list[int], fields: str) -> str:
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


def fetch(prs: list[int], *, stored: Mapping[int, Pr] | None = None,
          retry_waits: Sequence[float] = ()) -> tuple[dict[int, dict], set[int]]:
    """Fetch live head-bound facts for ``prs``, probing first each one in
    ``stored``. A probe-confirmed PR's facts carry no CI, diffstat or test keys.

    Returns ``(facts, not_found)``: facts keyed by PR number, plus the requested
    numbers GitHub explicitly reported NOT_FOUND — the PR no longer exists
    upstream (scrubbed/deleted), a deterministic verdict callers may act on.
    A number in neither is a transient miss (failed request, other GraphQL
    error) and remains eligible for a retry. A batch whose request failed is
    read again after the first pass in batches of ``RETRY_CHUNK_SIZE``, which
    GitHub's ten-second query limit is less likely to cut off. A rate-limit
    refusal ends the fetch: every later request would meet the same spent budget.
    """
    out: dict[int, dict] = {}
    not_found: set[int] = set()
    required_by_base: dict[str, list[str] | None] = {}
    failed: list[int] = []
    stored = stored or {}
    known = [n for n in prs if n in stored]
    full = prs
    retry: progress.Progress | None = None
    try:
        if known:
            probe = _report("probing live state and CI basis of", known, PROBE_CHUNK_SIZE)
            for i in range(0, len(known), PROBE_CHUNK_SIZE):
                chunk = known[i:i + PROBE_CHUNK_SIZE]
                _probe(chunk, stored, out, not_found, retry_waits)
                if probe is not None:
                    probe.advance(len(chunk))
            full = [n for n in prs if n not in out and n not in not_found]
            if probe is not None:
                probe.finish(f"{len(out):,} unchanged, {len(full):,} to read in full")
        report = _report("fetching live CI and mergeability for", full, CHUNK_SIZE)
        for i in range(0, len(full), CHUNK_SIZE):
            chunk = full[i:i + CHUNK_SIZE]
            if not _read(chunk, out, not_found, required_by_base, retry_waits):
                failed += chunk
            if report is not None:
                report.advance(len(chunk))
        if report is not None:
            report.finish(f"{sum(n in out for n in full):,} read"
                          + (f", {len(failed):,} to retry" if failed else ""))
        if failed:
            retry = (progress.Progress("retrying", len(failed),
                                       f"PRs, {RETRY_CHUNK_SIZE} at a time")
                     if report is not None else None)
            for i in range(0, len(failed), RETRY_CHUNK_SIZE):
                chunk = failed[i:i + RETRY_CHUNK_SIZE]
                _read(chunk, out, not_found, required_by_base, retry_waits)
                if retry is not None:
                    retry.advance(len(chunk))
    except RateLimited as exc:
        _log.warning("live PR fetch stopped: GitHub rate limit (%s); %d of %d PRs unread",
                     exc, len(prs) - len(out), len(prs))
    if retry is not None:
        recovered = sum(n in out for n in failed)
        retry.finish(f"{recovered:,} recovered, {len(failed) - recovered:,} still missing")
    return out, not_found


def _report(label: str, prs: list[int], chunk_size: int) -> progress.Progress | None:
    # Reported only in a job, and only across several requests: the app server
    # makes this fetch for its own reads.
    return (progress.Progress(label, len(prs), "PRs")
            if progress.enabled() and len(prs) > chunk_size else None)


def _state(node: dict) -> dict:
    return {"state": (node.get("state") or "").lower(),
            "merged": bool(node.get("merged")),
            "head": node.get("headRefOid"),
            "mergeable": node.get("mergeable"),
            "updated_at": node.get("updatedAt"),
            "maintainer_can_modify": node.get("maintainerCanModify")}


def _probe(chunk: list[int], stored: Mapping[int, Pr], out: dict[int, dict],
           not_found: set[int], retry_waits: Sequence[float]) -> None:
    """Probe one batch, keeping in ``out`` only the PRs it confirms. The rest,
    and every PR of a failed request, are left for the full read."""
    payload = gh_graphql(_query(chunk, _PROBE_FIELDS), retry_waits=retry_waits,
                         raise_on_rate_limit=True)
    if payload is None:
        return
    not_found |= _not_found_numbers(payload.get("errors") or [], chunk)
    repo = (payload.get("data") or {}).get("repository") or {}
    for j, n in enumerate(chunk):
        node = repo.get(f"p{j}")
        if isinstance(node, dict) and freshness.ci_confirmed(
                stored[n], node.get("headRefOid"), ci_signal.basis(node), node.get("lastEditedAt")):
            out[n] = _state(node)


def _read(chunk: list[int], out: dict[int, dict], not_found: set[int],
          required_by_base: dict[str, list[str] | None],
          retry_waits: Sequence[float]) -> bool:
    """Read one batch into ``out`` and ``not_found``. False when the request
    returned nothing usable."""
    payload = gh_graphql(_query(chunk, _FIELDS), retry_waits=retry_waits,
                         raise_on_rate_limit=True)
    if payload is None:
        _log.warning("live PR fetch failed for %d PRs (%d-%d)",
                     len(chunk), chunk[0], chunk[-1])
        return False
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
        complete = complete_pr_checks(node, retry_waits=retry_waits)
        runs, statuses = ci_signal.from_graphql_pr(node)
        required = (required_checks(node.get("baseRefName"), required_by_base,
                                    retry_waits=retry_waits) if complete else None)
        file_nodes = ((node.get("files") or {}).get("nodes")) or []
        paths = [f.get("path") for f in file_nodes if f.get("path")]
        out[int(n)] = {
            **_state(node),
            "ci": ci_signal.verdict(runs, statuses, required=required,
                                    awaiting_approval=ci_signal.awaiting_approval(node)),
            "unreported": ci_signal.unreported(runs, statuses, required or ()),
            "ci_failures": {**ci_signal.failure_facts(runs, statuses, node.get("lastEditedAt")),
                            "basis": ci_signal.basis(node)},
            "check_runs": runs,
            "statuses": statuses,
            "diffstat": diffstat,
            "has_tests": diffpaths.has_tests(paths),
        }
    return True
