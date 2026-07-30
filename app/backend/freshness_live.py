"""Live PR state (#25): fetch, compare, and persist.

The app's data is a snapshot. Master moves, PRs get force-pushed, closed, or
merged underneath us — and acting on a stale picture is how you embarrass a
contributor with a comment that contradicts reality. This module re-fetches the
live state for a batch of PRs, reports where it has DIVERGED from the snapshot,
and persists GitHub-owned drift (open/closed/merged + mergeable) into the shared
store so every operator converges on GitHub's truth by reading the store — no
per-machine overlay.

The GraphQL fetch is read-only, batched one request per ~40 PRs so it stays
rate-limit friendly even on a 50-row page. Only the divergent-state persistence
writes, and only to our own store (never upstream).
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.backend import data
from app.backend.safety_guard import run
from pipeline import diffpaths
from pipeline.settings import REPO_NAME, REPO_OWNER

if TYPE_CHECKING:
    from pipeline.model import Pr

_log = logging.getLogger(__name__)

_CHUNK = 40

_LIVE_MERGEABLE = {"MERGEABLE": True, "CONFLICTING": False}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone(timezone.utc)


def _live_state(lv: dict) -> str | None:
    """The normalized open/closed/merged state from a live_states entry — a merge
    outranks a raw state, matching GitHub's own merged→closed."""
    return "merged" if lv.get("merged") else (lv.get("state") or None)

# GraphQL status-rollup → our CI vocabulary
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


def live_states(prs: list[int]) -> dict[int, dict]:
    """Batched live {state, merged, head, mergeable, ci, diffstat, has_tests} per PR via GraphQL."""
    out: dict[int, dict] = {}
    for i in range(0, len(prs), _CHUNK):
        chunk = prs[i:i + _CHUNK]
        try:
            r = run(["gh", "api", "graphql", "-f", f"query={_query(chunk)}"], timeout=60)
        except subprocess.TimeoutExpired:
            # gh wedged; skip this chunk so the sweep stays bounded. Logged so a
            # slow/flaky gh is visible rather than live state silently lagging.
            _log.warning("live_states: gh graphql timed out; skipping %d PRs (%d-%d)",
                         len(chunk), chunk[0], chunk[-1])
            continue
        if r.returncode != 0:
            continue
        try:
            repo = (json.loads(r.stdout).get("data") or {}).get("repository") or {}
        except (ValueError, TypeError):
            continue
        for j, n in enumerate(chunk):
            node = repo.get(f"p{j}")
            if not node:
                continue
            nodes = ((node.get("commits") or {}).get("nodes")) or [{}]
            roll = (nodes[0] or {}).get("commit", {}).get("statusCheckRollup") or {}
            file_nodes = ((node.get("files") or {}).get("nodes")) or []
            paths = [f.get("path") for f in file_nodes if f.get("path")]
            out[int(n)] = {
                "state": (node.get("state") or "").lower(),   # open / closed / merged
                "merged": bool(node.get("merged")),
                "head": node.get("headRefOid"),
                "mergeable": node.get("mergeable"),            # MERGEABLE / CONFLICTING / UNKNOWN
                "ci": _CI_NORM.get(roll.get("state") or ""),
                "diffstat": {"additions": node.get("additions"),
                             "deletions": node.get("deletions"),
                             "changed_files": node.get("changedFiles")},
                "has_tests": diffpaths.has_tests(paths),
            }
    return out


def check(prs: list[int]) -> dict:
    """Compare live state against the store snapshot; return per-PR divergences.

    Also reconciles observed terminal state into our own store (#107): a PR that
    is closed/merged upstream gets its meta.state persisted, so it drops out of
    the active queue/cluster and into the 'resolved' lane — we don't just flag
    the divergence, we move the PR through our data set."""
    prs = [int(n) for n in prs][:300]
    live = live_states(prs)
    committed: dict[int, Pr] = data.prs()
    items = []
    for n in prs:
        lv = live.get(n)
        if not lv:
            items.append({"number": n, "reachable": False, "diverged": []})
            continue
        rec = committed.get(n)
        if rec is None:
            items.append({"number": n, "reachable": False, "diverged": []})
            continue
        meta = rec.section("meta") or {}
        sig = rec.signals or {}
        diverged = []

        if lv["merged"] and meta.get("state") != "merged":
            diverged.append({"kind": "merged",
                             "message": "merged upstream — your action is no longer needed"})
        elif lv["state"] and meta.get("state") and lv["state"] != meta["state"]:
            diverged.append({"kind": "state", "was": meta["state"], "now": lv["state"],
                             "message": f"now {lv['state']} upstream (was {meta['state']} when analyzed)"})

        if lv["head"] and meta.get("head_sha") and lv["head"] != meta["head_sha"]:
            diverged.append({"kind": "head", "was": meta["head_sha"][:7], "now": lv["head"][:7],
                             "message": "new commits since this was analyzed — the diff may be out of date"})

        if lv["mergeable"] == "CONFLICTING" and sig.get("mergeable"):
            diverged.append({"kind": "conflicts",
                             "message": "now has merge conflicts (was clean when this cluster was analyzed)"})

        # only flag a CI *change* when we had a real baseline to change from —
        # "unknown" means we never resolved CI, so unknown→passing isn't drift.
        if lv["ci"] and sig.get("ci") not in (None, "unknown") and lv["ci"] != sig["ci"]:
            diverged.append({"kind": "ci", "was": sig["ci"], "now": lv["ci"],
                             "message": f"CI is now {lv['ci']} (was {sig['ci']})"})

        items.append({"number": n, "reachable": True, "diverged": diverged,
                      "state": lv["state"], "head": lv["head"]})
    # persist the observed live state into the shared store so the next read
    # reflects it (closed/merged PRs drop out of the active queue) and every
    # operator sees it — never an upstream write.
    persist_live(live, committed)
    data.refresh()
    return {"items": items}


# --- persist observed drift into the shared store --------------------------
def persist_live(live: dict[int, dict], committed: dict[int, Pr]) -> list[int]:
    """Write each PR's live state (open/closed/merged), mergeable verdict, diffstat,
    and has_tests into the shared store when they diverge from the committed values
    in `committed` (the snapshot). Skips PRs with no store row (never ingested — nothing
    to update). Returns the PRs actually written, sorted."""
    store = data.store()
    changed: list[int] = []
    with store.batch():
        for n, lv in (live or {}).items():
            n = int(n)
            pr = committed.get(n)
            sig = (pr.signals if pr else None) or {}
            state = _live_state(lv)
            state_arg = state if (state and state != (pr.state if pr else None)) else None
            live_mrg = _LIVE_MERGEABLE.get(lv.get("mergeable") or "")
            mergeable_arg = (live_mrg if (live_mrg is not None
                                          and live_mrg != (pr.mergeable if pr else None)) else None)
            live_ds = lv.get("diffstat")
            diffstat_arg = live_ds if (live_ds is not None and live_ds != sig.get("diffstat")) else None
            live_ht = lv.get("has_tests")
            has_tests_arg = live_ht if (live_ht is not None and live_ht != sig.get("has_tests")) else None
            if (state_arg is None and mergeable_arg is None
                    and diffstat_arg is None and has_tests_arg is None):
                continue
            if store.load_pr(n) is None:
                continue
            store.edit_pr(n).record_live_state(state=state_arg, mergeable=mergeable_arg,
                                               diffstat=diffstat_arg, has_tests=has_tests_arg)
            changed.append(n)
    return sorted(changed)


# --- the launch / manual sweep ---------------------------------------------
def sweep(prs: list[int] | None = None) -> dict:
    """Fetch live state for the open PR universe (or a given subset) and persist any
    drift into the shared store. Reads the committed snapshot the app already
    loaded (`data.prs()`) — no second bulk download — and targets PRs the store
    thinks are open (`state`), so a PR reopened upstream (state now open) is still
    re-checked. Stamps the shared `live_sweep` singleton. Returns
    {checked, changed, prs, fetched_at}."""
    from app.backend import activity
    snapshot = data.prs()
    if prs is None:
        # store-open PRs, plus the PRs we've closed — a closed PR is off the open
        # set, so re-checking the closed-by-us set is how an upstream reopen lands
        # back in the store (and surfaces in the reopened-after-close panel).
        targets = sorted({n for n, pr in snapshot.items() if pr.state == "open"}
                         | {n for n in activity.closed_by_us_prs() if n in snapshot})
    else:
        targets = [int(n) for n in prs if int(n) in snapshot]
    live = live_states(targets)
    changed = persist_live(live, snapshot)
    swept_at = _now()
    data.store().save_live_sweep({"swept_at": swept_at})
    return {"checked": len(targets), "changed": len(changed),
            "prs": changed, "fetched_at": swept_at}


def last_swept_at() -> str | None:
    """When the live sweep last ran, from the shared store singleton — drives the
    'live as of …' UI and the launch-time re-sweep gate."""
    return data.store().load_live_sweep().get("swept_at")


def stale(ttl_min: float) -> bool:
    """True when the store has no sweep on record or the last one is older than
    ttl_min — i.e. worth a sweep. Lets the launch hook reuse a recent sweep (shared
    across operators) instead of re-querying GitHub on every relaunch (~70 GraphQL
    calls)."""
    at = _dt(last_swept_at())
    if at is None:
        return True
    return (datetime.now(timezone.utc) - at).total_seconds() / 60 >= ttl_min
