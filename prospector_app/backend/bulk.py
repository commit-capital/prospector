# prospector_app/backend/bulk.py
"""Apply one triage action to many PRs, streamed as SSE.

Bulk is pure orchestration: it loops the selected PR numbers through the same
per-PR executor, queue, and allowlisted-job paths used elsewhere in the app.
Upstream writes retain their per-PR gates and activity-log entries, and local
verification/security work retains its normal queue and job controls.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from prospector_app.backend import executor
from prospector_app.backend import jobs
from prospector_app.backend import models
from prospector_app.backend import review_refresh
from prospector_app.backend import training
from pipeline import review_policy
from pipeline import reviewers
from prospector_app.backend import verify_queue

CAP = 1000

_REVIEW = {"APPROVE": "approve", "REQUEST_CHANGES": "request-changes", "COMMENT": "comment"}

# Cluster dispositions (the raw UI strings) → the executor's vocabulary.
_CLUSTER_REVIEW = ("request-changes", "approve", "comment")  # submit_review events
_CLUSTER_CLOSE = {"close-dup": "CLOSE_DUP", "close-fixed": "CLOSE_FIXED",
                  "close-stale": "CLOSE_STALE", "close": "CLOSE"}


def _warn_bookkeeping(done: dict[str, Any], errors: list[str]) -> None:
    """Attach a batch-level warning to the done frame when any action executed
    upstream but its post-write bookkeeping (store reflection / activity append)
    failed. The upstream writes landed; the warning tells the operator the
    app's own records for those PRs are behind, and names the first error —
    a store-level failure (e.g. a stale schema guard) repeats across the whole
    batch, so one loud message beats a per-row detail hunt."""
    if not errors:
        return
    done["bookkeeping_errors"] = len(errors)
    done["warning"] = (
        f"{len(errors)} action(s) executed upstream but post-write bookkeeping "
        f"failed ({errors[0]}) — the store/activity records for them are stale. "
        "Fix the cause (a stale-schema error means: pull + restart the app), "
        "then reconcile with a live sweep / activity backfill.")


async def run_bulk(prs: list[int], action: str, *, comment: str | None = None,
                   comments: dict[int, str] | None = None,
                   canonical: int | None = None, method: str = "squash",
                   reason: str | None = None, tags: list[str] | None = None,
                   reviewer: str | None = None,
                   dry_run: bool = True) -> AsyncIterator[dict[str, Any]]:
    """Yield {'event','data'} SSE frames: one 'result' per PR, then 'done' with a
    summary. A too-large batch yields a single 'error' and stops.

    `comments` is an optional per-PR comment override (PR number → text), so a
    bulk close can post each PR's own suggested comment instead of one shared
    `comment` for all of them (#182). A PR absent from the map falls back to the
    shared `comment` (and then, in the executor, to the default template)."""
    if len(prs) > CAP:
        yield {"event": "error", "data": f"batch of {len(prs)} exceeds cap of {CAP}"}
        return

    # Local background actions use no upstream token.
    token = None if dry_run or action in ("QUEUE_VERIFY", "RUN_SECURITY") else executor.mint_bot_token()
    summary: dict[str, int] = {}
    bookkeeping_errors: list[str] = []
    for n in prs:
        pr_comment = (comments or {}).get(n) or comment
        try:
            if action == "RUN_SECURITY":
                try:
                    job = jobs.start_job("security-review", pr=n)
                    jobs.schedule_job(job)
                    res = {"pr": n, "action": action, "status": "queued",
                           "job_id": job["id"],
                           "detail": f"security review queued as background job #{job['id']}"}
                except ValueError as e:
                    res = {"pr": n, "action": action, "status": "skipped",
                           "detail": str(e)}
            elif action == "QUEUE_VERIFY":
                # A local write to the shared verify queue; the sandbox worker
                # picks it up. Nothing is posted upstream, so dry-run does not
                # apply. The pre-check's operator-readable refusal (already in
                # flight, closed, threat-flagged) reads as skipped, not error.
                try:
                    verify_queue.queue_pr(n)
                    res = {"pr": n, "action": action, "status": "queued",
                           "detail": "queued for sandbox verification"}
                except ValueError as e:
                    res = {"pr": n, "action": action, "status": "skipped",
                           "detail": str(e)}
            elif action == "MERGE":
                res = executor.merge_pr(n, method, dry_run=dry_run, reason=reason)
            elif action == "REVIEW_RETRIGGER":
                # The request names a reviewer; only the registry's own id for it
                # goes any further.
                named = next((r for r in reviewers.REVIEWERS.values() if r.id == reviewer), None)
                chosen = named or next(
                    (r for r in review_policy.active_reviewers(reviewers.REVIEW)
                     if r.retrigger_mention), None)
                rid = chosen.id if chosen is not None else ""
                baseline = review_refresh.capture(n, rid) if token is not None and rid else None
                res = executor.retrigger_review(n, rid, token=token, dry_run=dry_run)
                if res.get("status") == "executed" and baseline is not None:
                    review_refresh.schedule(n, rid, baseline)
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
        if res.get("bookkeeping_error"):
            bookkeeping_errors.append(str(res["bookkeeping_error"]))
        yield {"event": "result", "data": json.dumps(res)}
    done: dict[str, Any] = {"summary": summary}
    _warn_bookkeeping(done, bookkeeping_errors)
    yield {"event": "done", "data": json.dumps(done)}


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
    bookkeeping_errors: list[str] = []

    def tally(res: dict[str, Any]) -> dict[str, str]:
        status = res.get("status", "error")
        summary[status] = summary.get(status, 0) + 1
        if res.get("bookkeeping_error"):
            bookkeeping_errors.append(str(res["bookkeeping_error"]))
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
    _warn_bookkeeping(done, bookkeeping_errors)
    yield {"event": "done", "data": json.dumps(done)}
