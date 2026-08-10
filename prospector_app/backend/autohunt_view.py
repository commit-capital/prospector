"""The app's auto-hunt surface: the idle hunter's status (worker opt-in,
pool sizes, failure memory), a windowed result summary, and its run history
from the store's runs ledger."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, TypedDict

from pipeline import gates
from pipeline import storekit
from prospector_app.backend import data
from prospector_app.backend import verify_queue
from prospector_app.backend import verify_worker

if TYPE_CHECKING:
    from pipeline.model import Pr


class AutohuntRun(TypedDict):
    phase: str
    pr: int
    title: str | None
    started: str | None
    finished: str | None
    trigger: str | None
    result: str | None


class VerifyFailed(TypedDict):
    pr: int
    error_kind: str | None
    source: str | None


class AutohuntResultCounts(TypedDict):
    total: int
    by_result: dict[str, int]
    pr_ids_by_result: dict[str, list[int]]


class AutohuntSummary(TypedDict):
    days: int | None  # None means the whole ledger (the "all time" window)
    security: AutohuntResultCounts
    verify: AutohuntResultCounts


class AutohuntStatus(TypedDict):
    enabled: bool
    runner: dict
    security_pool: int
    verify_pool: int
    security_failed: list[int]
    verify_failed: list[VerifyFailed]


# ledger phase -> the lane name the panel renders
_HISTORY_PHASES = {"security:review-one": "security", "verify:single": "verify"}


def status() -> AutohuntStatus:
    """The hunter's live status. `enabled` and `security_failed` come from the
    worker's heartbeat registry, so any app reports the runner machine's
    state; the pool counts are computed from the shared snapshot with the same
    gates and failure-memory the hunter picks by. A PR parked in
    `security_failed` is not counted in `security_pool` — it is not awaiting
    pickup, and renders separately as a failed chip. `verify_failed` lists every
    verify request that ended in error, auto-queued or operator-queued alike —
    the hunter never re-fires an errored request, so each waits for an operator
    re-queue regardless of who queued it — tagged with its `source` ("auto" for
    hunter-fired requests, None for operator-queued ones) so the panel can label
    each failure by who queued it."""
    records = verify_queue.worker_records(data.store().load_verify_worker())
    newest: dict = records[0] if records else {}
    prs = data.prs()
    failed = newest.get("security_failed")
    failed_set = {int(n) for n in failed} if isinstance(failed, list) else set()
    verify_failed: list[VerifyFailed] = []
    for n, pr in sorted(prs.items()):
        req = pr.verify_request or {}
        if req.get("status") == "error":
            verify_failed.append({"pr": n, "error_kind": req.get("error_kind"),
                                  "source": req.get("source")})
    return {
        "enabled": bool(newest.get("autohunt")),
        "runner": verify_queue.runner_status(),
        "security_pool": sum(1 for n, pr in prs.items()
                             if n not in failed_set and gates.blocked_on_security(pr)),
        "verify_pool": sum(1 for pr in prs.values() if verify_worker.auto_verifiable(pr)),
        "security_failed": sorted(failed_set),
        "verify_failed": verify_failed,
    }


def _result(lane: str, rec: dict) -> str | None:
    """One display word for what a run concluded: the security verdict, or —
    for a verify run — the state it ended in.

    `done` is the only status whose run committed the entry's outcome, so it is
    the only one that reports it. Every other state (a `waiting-for-base` park,
    an `error:<kind>`, a cancel, a transient re-queue) is the state the run
    itself reached; an entry in that state can still carry an outcome stat, and
    that word belongs to whichever earlier run left it on the PR."""
    stats = rec.get("stats") or {}
    if lane == "security":
        verdict = stats.get("verdict")
        return str(verdict) if verdict is not None else None
    status_ = stats.get("status")
    if status_ == "error":
        kind = stats.get("error_kind")
        return f"error:{kind}" if kind else "error"
    if status_ is not None and status_ != "done":
        return str(status_)
    outcome = stats.get("outcome")
    if outcome is not None:
        return str(outcome)
    return str(status_) if status_ is not None else None


# A bounded tail of the runs ledger to scan for history(): the ledger
# interleaves phases besides security/verify (ingest, cluster, ...), so the
# tail read must run well past the `limit` shown to still catch enough
# matching entries.
HISTORY_TAIL = 500

# Hard cap on rows history_window() returns, even inside a wide day window —
# so a busy hunter can't hand the panel thousands of rows to render.
HISTORY_LIMIT_CAP = 300


def _cutoff(days: int | None) -> str | None:
    """The ISO instant `days` days before now, or None for no lower bound (the
    "all time" window) — the `since` argument to `data.runs`."""
    if days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _row(rec: storekit.RunRecord, prs: dict[int, Pr],
         lanes: frozenset[str] | None) -> AutohuntRun | None:
    """One ledger record as a panel row, or None when it's not a security/verify
    phase run (the ledger interleaves other phases and store-edits too) or its
    lane isn't in `lanes` (None means every lane)."""
    if not isinstance(rec, storekit.PhaseRun):
        return None
    lane = _HISTORY_PHASES.get(rec.phase)
    n = rec.raw.get("pr")
    if lane is None or not isinstance(n, int):
        return None
    if lanes is not None and lane not in lanes:
        return None
    pr = prs.get(n)
    return {
        "phase": lane, "pr": n,
        "title": pr.title if pr is not None else None,
        "started": rec.started, "finished": rec.finished,
        "trigger": rec.raw.get("trigger"), "result": _result(lane, rec.raw)}


def history(limit: int = 50) -> list[AutohuntRun]:
    """The most recent per-PR security/verify runs from the runs ledger,
    newest first. `trigger` is "autohunt" on hunter-fired runs and None on
    operator-fired or unstamped entries; timestamps are None on entries
    recorded without them. Scans a bounded tail of the ledger (HISTORY_TAIL),
    not the whole table."""
    prs = data.prs()
    out: list[AutohuntRun] = []
    for rec in reversed(data.runs(limit=HISTORY_TAIL)):
        row = _row(rec, prs, None)
        if row is None:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def history_window(days: int | None, limit: int = 100,
                    lanes: frozenset[str] | None = None) -> list[AutohuntRun]:
    """The most recent per-PR security/verify runs within the last `days` days
    (None = every run in the ledger), newest first, capped at `limit` (and
    HISTORY_LIMIT_CAP regardless of what's asked). Filters the ledger by its
    indexed `ts` column, so — unlike `history`'s bounded tail scan — every
    matching run in the window is found regardless of how it's interleaved
    with other phases. `lanes` restricts the result to those lanes (None is
    both security and verify)."""
    limit = min(limit, HISTORY_LIMIT_CAP)
    prs = data.prs()
    out: list[AutohuntRun] = []
    for rec in reversed(data.runs(since=_cutoff(days))):
        row = _row(rec, prs, lanes)
        if row is None:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def summary(days: int | None) -> AutohuntSummary:
    """Security/verify run counts and result breakdowns over the last `days`
    days (None = the whole ledger) — the auto-hunt panel's default view, a
    fixed-size digest in place of an ever-growing per-run table. Filters the
    runs ledger by its indexed `ts` column, so the scan cost tracks the
    window, not the ledger's total size. Each bucket also carries the distinct
    PR numbers behind it, so the app can open exactly those PRs in the
    Explorer when an operator clicks a result chip."""
    totals = {"security": 0, "verify": 0}
    by_result: dict[str, dict[str, int]] = {"security": {}, "verify": {}}
    pr_ids_by_result: dict[str, dict[str, list[int]]] = {"security": {}, "verify": {}}
    for rec in data.runs(since=_cutoff(days)):
        if not isinstance(rec, storekit.PhaseRun):
            continue
        lane = _HISTORY_PHASES.get(rec.phase)
        if lane is None:
            continue
        totals[lane] += 1
        result = _result(lane, rec.raw) or "—"
        by_result[lane][result] = by_result[lane].get(result, 0) + 1
        n = rec.raw.get("pr")
        if isinstance(n, int):
            ids = pr_ids_by_result[lane].setdefault(result, [])
            if n not in ids:
                ids.append(n)
    for lane in ("security", "verify"):
        for result, ids in pr_ids_by_result[lane].items():
            ids.sort()
    return {
        "days": days,
        "security": {"total": totals["security"], "by_result": by_result["security"],
                     "pr_ids_by_result": pr_ids_by_result["security"]},
        "verify": {"total": totals["verify"], "by_result": by_result["verify"],
                   "pr_ids_by_result": pr_ids_by_result["verify"]},
    }
