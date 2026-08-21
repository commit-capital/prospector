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

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from prospector_app.backend import data
from pipeline import live_prs
from pipeline import review_policy
from pipeline import reviewers

if TYPE_CHECKING:
    from pipeline.model import Pr

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


def live_states(prs: list[int]) -> tuple[dict[int, dict], set[int]]:
    """Batched live {state, merged, head, mergeable, ci, diffstat, has_tests} per
    PR via GraphQL, plus the set of requested numbers GitHub reports NOT_FOUND
    (the PR was deleted upstream)."""
    return live_prs.fetch(prs)


def check(prs: list[int]) -> dict:
    """Compare live state against the store snapshot; return per-PR divergences.

    Also reconciles observed terminal state into our own store (#107): a PR that
    is closed/merged upstream gets its meta.state persisted, so it drops out of
    the active queue/cluster and into the 'resolved' lane — we don't just flag
    the divergence, we move the PR through our data set."""
    prs = [int(n) for n in prs][:300]
    live, _ = live_states(prs)
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

        # Every active reviewer's bar is a hard merge requirement, so a verdict
        # against an earlier commit is its own staleness — separate from `head`
        # because the remedy is a review re-trigger, not a re-analysis.
        for r in review_policy.active_reviewers(reviewers.REVIEW):
            reviewed = (rec.review_entry(r.id) or {}).get("reviewed_sha")
            if lv["head"] and reviewed and reviewed != lv["head"]:
                diverged.append({"kind": "review", "reviewer": r.id, "label": r.label,
                                 "was": reviewed[:7], "now": lv["head"][:7],
                                 "message": f"{r.label} reviewed an earlier commit — its verdict "
                                            "may not reflect the current diff"})

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
    """Write each PR's live state and current-head signals into the shared store.

    CI, mergeability, diffstat, and test presence are persisted only when the
    fetched head matches the committed head. Missing live values preserve the
    stored verdict. PRs with no store row are skipped. Returns the PRs actually
    written, sorted.
    """
    store = data.store()
    changed: list[int] = []
    with store.batch():
        for n, lv in (live or {}).items():
            n = int(n)
            pr = committed.get(n)
            sig = (pr.signals if pr else None) or {}
            state = _live_state(lv)
            state_arg = state if (state and state != (pr.state if pr else None)) else None
            same_head = pr is not None and lv.get("head") == pr.head_sha
            live_ci = lv.get("ci") if same_head else None
            ci_arg = live_ci if live_ci is not None and live_ci != sig.get("ci") else None
            live_mrg = _LIVE_MERGEABLE.get(lv.get("mergeable") or "") if same_head else None
            mergeable_arg = (live_mrg if live_mrg is not None
                              and live_mrg != (pr.mergeable if pr else None) else None)
            live_ds = lv.get("diffstat") if same_head else None
            diffstat_arg = (live_ds if live_ds is not None
                            and live_ds != sig.get("diffstat") else None)
            live_ht = lv.get("has_tests") if same_head else None
            has_tests_arg = (live_ht if live_ht is not None
                             and live_ht != sig.get("has_tests") else None)
            # The observed head is the one fact worth writing when nothing else
            # moved: it is what makes a push the pipeline has not ingested read
            # stale everywhere. Retained only while it differs from the ingested
            # head, so pass it whenever that retention would change.
            observed_head = lv.get("head")
            retained = (observed_head if observed_head and pr is not None
                        and observed_head != pr.head_sha else None)
            live_head_arg = (observed_head if observed_head and pr is not None
                             and retained != pr.live_head_sha else None)
            if (state_arg is None and ci_arg is None and mergeable_arg is None
                    and diffstat_arg is None and has_tests_arg is None
                    and live_head_arg is None):
                continue
            if store.load_pr(n) is None:
                continue
            store.edit_pr(n).record_live_state(
                state=state_arg, ci=ci_arg, mergeable=mergeable_arg,
                diffstat=diffstat_arg, has_tests=has_tests_arg,
                live_head_sha=live_head_arg)
            changed.append(n)
    return sorted(changed)


# --- retire PRs deleted upstream --------------------------------------------
def retire_unresolvable(prs: set[int], committed: dict[int, Pr]) -> list[int]:
    """Mark each store-known PR in `prs` unresolvable — GitHub reports the number
    cannot resolve to a PullRequest, so the PR is gone upstream (e.g. a spam
    scrub). An open one is closed in the same write; later sweeps drop the PR
    from their target sets. Returns the PRs marked, sorted."""
    store = data.store()
    retired: list[int] = []
    for n in sorted(prs):
        if n not in committed:
            continue
        store.edit_pr(n).record_unresolvable()
        retired.append(n)
    return retired


# --- the launch / manual sweep ---------------------------------------------
def sweep(prs: list[int] | None = None) -> dict:
    """Fetch live state for the open PR universe (or a given subset) and persist any
    drift into the shared store. Reads the committed snapshot the app already
    loaded (`data.prs()`) — no second bulk download — and targets PRs the store
    thinks are open (`state`), so a PR reopened upstream (state now open) is still
    re-checked. A PR GitHub reports NOT_FOUND is retired (closed + marked
    unresolvable) and counts as handled, not failed. A complete full-corpus pass
    stamps the shared `live_sweep` singleton. Returns attempted/checked counts,
    changed, failed, and retired PR IDs, completion, and fetched_at."""
    from prospector_app.backend import activity
    snapshot = data.prs()
    full_sweep = prs is None
    if full_sweep:
        # store-open PRs, plus the PRs we've closed — a closed PR is off the open
        # set, so re-checking the closed-by-us set is how an upstream reopen lands
        # back in the store (and surfaces in the reopened-after-close panel).
        # Unresolvable PRs are gone upstream; no fetch can reach them.
        targets = sorted({n for n, pr in snapshot.items()
                          if pr.state == "open" and not pr.unresolvable}
                         | {n for n in activity.closed_by_us_prs()
                            if n in snapshot and not snapshot[n].unresolvable})
    else:
        targets = [int(n) for n in prs if int(n) in snapshot]
    target_set = set(targets)
    fetched, nf = live_states(targets)
    live = {n: facts for n, facts in fetched.items() if n in target_set}
    not_found = nf & target_set
    missing = sorted(target_set - set(live) - not_found)
    if missing:
        fetched, nf = live_states(missing)
        live.update({n: facts for n, facts in fetched.items() if n in target_set})
        not_found |= nf & target_set
    retired = retire_unresolvable(not_found, snapshot)
    missing = sorted(target_set - set(live) - not_found)
    changed = persist_live(live, snapshot)
    # Publish what was just written before anyone reads again: the next sweep
    # compares against it to stay idempotent, and the app serves the observed
    # heads instead of waiting for the debounced background check.
    data.refresh()
    swept_at = _now()
    complete = not missing
    if full_sweep and complete:
        data.store().save_live_sweep({"swept_at": swept_at})
    return {"attempted": len(targets), "checked": len(live), "changed": len(changed),
            "prs": changed, "failed": missing, "retired": retired,
            "complete": complete, "fetched_at": swept_at}


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
