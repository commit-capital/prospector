# review_cockpit/backend/bulk.py
"""Apply one triage action to many PRs, streamed as SSE.

Bulk is pure orchestration: it loops the SELECTED PR numbers through the same
per-PR executor functions the single-PR action bar uses, so every per-PR gate
(gates.merge_eligibility, the configured-bot empty-token write-gate, threat/secret
blocks, freshness) and every per-PR activity-log entry are reused unchanged.
There is no bulk-specific write path and no way to bypass a gate.
"""
from __future__ import annotations

import json
from typing import Any

from review_cockpit.backend import executor
from review_cockpit.backend import models
from review_cockpit.backend import training

CAP = 1000

_REVIEW = {"APPROVE": "approve", "REQUEST_CHANGES": "request-changes", "COMMENT": "comment"}

# Cluster dispositions (the raw UI strings) → the executor's vocabulary.
_CLUSTER_REVIEW = ("request-changes", "approve", "comment")  # submit_review events
_CLUSTER_CLOSE = {"close-dup": "CLOSE_DUP", "close-fixed": "CLOSE_FIXED",
                  "close-stale": "CLOSE_STALE", "close": "CLOSE"}


async def run_bulk(prs: list[int], action: str, *, comment: str | None = None,
                   comments: dict[int, str] | None = None,
                   canonical: int | None = None, method: str = "squash",
                   reason: str | None = None, tags: list[str] | None = None,
                   dry_run: bool = True):
    """Yield {'event','data'} SSE frames: one 'result' per PR, then 'done' with a
    summary. A too-large batch yields a single 'error' and stops.

    `comments` is an optional per-PR comment override (PR number → text), so a
    bulk close can post each PR's own suggested comment instead of one shared
    `comment` for all of them (#182). A PR absent from the map falls back to the
    shared `comment` (and then, in the executor, to the default template)."""
    if len(prs) > CAP:
        yield {"event": "error", "data": f"batch of {len(prs)} exceeds cap of {CAP}"}
        return

    token = None if dry_run else executor.mint_bot_token()
    summary: dict[str, int] = {}
    for n in prs:
        pr_comment = (comments or {}).get(n) or comment
        try:
            if action == "MERGE":
                res = executor.merge_pr(n, method, dry_run=dry_run, reason=reason)
            elif action in _REVIEW:
                res = executor.submit_review(n, _REVIEW[action], pr_comment or "",
                                             token=token, dry_run=dry_run)
            else:  # CLOSE / CLOSE_DUP / CLOSE_FIXED / CLOSE_STALE
                res = executor.execute_pr(n, models.CloseAction(
                    pr=n, action=action,
                    canonical=canonical, comment=pr_comment or None,
                    reason=reason, tags=tags,
                ), token=token, dry_run=dry_run)
        except Exception as e:  # one PR's failure never aborts the batch
            res = {"pr": n, "action": action, "status": "error", "detail": str(e)[:200]}
        status = res.get("status", "error")
        summary[status] = summary.get(status, 0) + 1
        yield {"event": "result", "data": json.dumps(res)}
    yield {"event": "done", "data": json.dumps({"summary": summary})}


def _merge_ok(res: dict[str, Any]) -> bool:
    """Whether a merge 'succeeded' enough that its dependent closes may proceed: it
    merged, previewed cleanly (dry-run), or was already merged upstream. Anything
    else means PRs pointing at it must NOT be closed as duplicates (#188)."""
    status = res.get("status")
    return (status in ("merged", "dry-run")
            or (status == "blocked" and "already merged" in (res.get("detail") or "").lower()))


def _run_cluster_item(item: models.ClusterItem, *, token: str | None, dry_run: bool) -> dict[str, Any]:
    """Dispatch one cluster item to the matching per-PR executor — normalizing its
    raw UI disposition to the executor vocabulary — and capture per-PR training data
    exactly as the single-PR endpoints do. One PR's failure never aborts the batch."""
    n = int(item.pr)
    action = (item.action or "").lower()
    comment = item.comment
    reason = item.reason
    tags = item.tags
    try:
        if action == "merge":
            res = executor.merge_pr(n, item.method or "squash", dry_run=dry_run, reason=reason)
        elif action in _CLUSTER_REVIEW:
            res = executor.submit_review(n, action, comment or "",
                                         token=token, dry_run=dry_run)
        elif action in _CLUSTER_CLOSE:
            res = executor.execute_pr(n, models.CloseAction(
                pr=n, action=_CLUSTER_CLOSE[action],
                canonical=item.canonical,
                upstream_pr=item.upstream_pr,
                upstream_commit=item.upstream_commit,
                upstream_date=item.upstream_date,
                comment=comment or None, reason=reason, tags=tags,
            ), token=token, dry_run=dry_run)
        else:
            res = {"pr": n, "action": action, "status": "skipped",
                   "detail": f"unknown disposition: {action!r}"}
    except Exception as e:
        res = {"pr": n, "action": action, "status": "error", "detail": str(e)[:200]}
    training.capture(n, res.get("action", action), reason=reason, tags=tags,
                     public_body=comment, dry_run=dry_run, result=res)
    return res


async def run_cluster(items: list[models.ClusterItem], *, dry_run: bool = True):
    """Apply each PR's own disposition across a cluster — the cluster page's mixed
    'execute all' (merge / request-changes / close-*) — streamed as SSE: one
    'result' frame per PR, then a 'done' summary. Each item is normalized from the
    raw UI disposition to the executor vocabulary and dispatched to the same per-PR
    executor functions the single-PR endpoints use; per-PR training data is captured
    exactly as those endpoints do. A too-large batch yields one 'error'.

    Merges run FIRST; if any fails, the dependent close/ask actions are skipped and
    the 'done' frame carries an `aborted` message, so we never close a PR as a
    duplicate of a PR that didn't actually merge (#188).

    Each item is a models.ClusterItem."""
    if len(items) > CAP:
        yield {"event": "error", "data": f"batch of {len(items)} exceeds cap of {CAP}"}
        return

    token = None if dry_run else executor.mint_bot_token()
    summary: dict[str, int] = {}

    def tally(res: dict[str, Any]) -> dict[str, str]:
        status = res.get("status", "error")
        summary[status] = summary.get(status, 0) + 1
        return {"event": "result", "data": json.dumps(res)}

    is_merge = lambda it: (it.action or "").lower() == "merge"
    merges = [it for it in items if is_merge(it)]
    rest = [it for it in items if not is_merge(it)]
    aborted: str | None = None
    merge_failed = False
    for item in merges:
        res = _run_cluster_item(item, token=token, dry_run=dry_run)
        merge_failed = merge_failed or not _merge_ok(res)
        yield tally(res)
    if merge_failed and rest:
        n = len(rest)
        aborted = (f"A merge failed — skipped {n} follow-up action{'' if n == 1 else 's'} "
                   "(closes/asks) so you don't close PRs as duplicates of a PR that didn't "
                   "merge. Fix the merge, then re-run.")
    else:
        for item in rest:
            yield tally(_run_cluster_item(item, token=token, dry_run=dry_run))
    done: dict[str, Any] = {"summary": summary}
    if aborted:
        done["aborted"] = aborted
    yield {"event": "done", "data": json.dumps(done)}
