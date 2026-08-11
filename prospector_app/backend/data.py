"""Read-side data access for Prospector.

Every board/list read is served from an in-memory snapshot of the store — the
SQL database behind pipeline's store.py / gates.py / freshness.py — so requests
do no per-call DB I/O. The snapshot self-freshens incrementally off the request
path (see the module-global note below); refresh() forces a full reload.
Read-only: upstream writes go through the executor and chat paths.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import authors
from pipeline import storekit
from pipeline.store import Store

if TYPE_CHECKING:
    from pipeline.model import Cluster, Pr

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = REPO_ROOT / "pipeline"

_store = Store()

# In-memory snapshot of the store. Every list/board read serves from these dicts
# with no per-request DB I/O; a background check (at most once per CHECK_DEBOUNCE
# seconds) refetches only rows written at or after the last watermark via
# store.*_since, so a slow store can never block or wedge a request — it only lets
# the snapshot lag by up to CHECK_DEBOUNCE seconds. The freshness check runs off
# the request path: readers always return the current snapshot immediately.
# Reclustering soft-deletes clusters (tombstones with a bumped saved_at), so a
# removal rides the same watermark and drops from the snapshot on the next check.
CHECK_DEBOUNCE = 10.0  # seconds

_prs: dict[int, Pr] = {}
_clusters: dict[int, Cluster] = {}
_pr_to_clusters_idx: dict[int, list[int]] = {}
_pr_watermark: str | None = None
_clu_watermark: str | None = None
_generation = 0
_loaded = False
_last_check = 0.0
_check_lock = threading.Lock()  # single-flights the freshen; never held by a reader
_author_baseline: dict | None = None  # inner `authors` map, read once
_author_table: dict[str, dict] | None = None  # per-snapshot leaderboard; invalidated on freshen


def store() -> Store:
    return _store


def _index_pr_to_clusters(clusters: dict[int, Cluster]) -> dict[int, list[int]]:
    """Map each PR to the sorted list of clusters it belongs to. A PR may straddle
    several clusters (#196)."""
    out: dict[int, list[int]] = {}
    for cid, c in clusters.items():
        for n in c.prs or []:
            out.setdefault(int(n), []).append(cid)
    for n in out:
        out[n].sort()
    return out


def _freshen(full: bool = False) -> None:
    """Refetch the rows changed since the last watermark (everything when `full`)
    and republish the snapshot. A change builds new dicts and rebinds the module
    globals — an atomic swap under the GIL, so a concurrent reader sees a whole
    old or whole new snapshot, never a half-mutated one and never blocks. A
    freshen that finds nothing changed keeps the current dict objects, so the
    snapshot's identity (and `generation()`) only moves when its contents do."""
    global _prs, _clusters, _pr_to_clusters_idx, _pr_watermark, _clu_watermark, \
        _author_table, _generation
    # full starts from empty (a true replace — drops rows deleted since), so the
    # watermark resets to the fetched max; incremental merges the delta onto the
    # current snapshot and advances the watermark.
    pr_delta, pr_hi = _store.prs_since(None if full else _pr_watermark)
    clu_delta, clu_deleted, clu_hi = _store.clusters_since(None if full else _clu_watermark)

    prs_changed = full or bool(pr_delta)
    clusters_changed = full or bool(clu_delta) or bool(clu_deleted)

    if prs_changed:
        _prs = dict(pr_delta) if full else {**_prs, **pr_delta}
    if pr_hi:
        _pr_watermark = pr_hi if (full or _pr_watermark is None) else max(_pr_watermark, pr_hi)

    if clusters_changed:
        new_clusters = dict(clu_delta) if full else {**_clusters, **clu_delta}
        # A recluster soft-deletes clusters; the tombstones ride the same watermark
        # and arrive as deleted ids, so drop them — the snapshot tracks removals,
        # not just inserts and updates.
        for cid in clu_deleted:
            new_clusters.pop(cid, None)
        _clusters = new_clusters
        _pr_to_clusters_idx = _index_pr_to_clusters(new_clusters)
    if clu_hi:
        _clu_watermark = clu_hi if (full or _clu_watermark is None) else max(_clu_watermark, clu_hi)
    if prs_changed or clusters_changed:
        _author_table = None  # PR snapshot changed → recompute the leaderboard lazily
        _generation += 1


def _ensure() -> None:
    """Make sure the snapshot is loaded and not too stale, without ever blocking a
    reader once loaded. The first call blocks on the one cold full-load; after
    that, a stale read kicks a background freshen and returns the current snapshot
    (the freshen updates it for next time)."""
    global _loaded, _last_check
    if not _loaded:
        with _check_lock:
            if not _loaded:
                _freshen(full=True)
                _loaded = True
                _last_check = time.monotonic()
        return
    if time.monotonic() - _last_check < CHECK_DEBOUNCE:
        return
    if not _check_lock.acquire(blocking=False):
        return  # a freshen is already in flight — serve the current snapshot
    _last_check = time.monotonic()  # from check start, so a burst spawns one thread

    def run() -> None:
        try:
            _freshen()
        except Exception:
            pass  # keep the current snapshot; the next window retries
        finally:
            _check_lock.release()

    threading.Thread(target=run, daemon=True, name="store-freshen").start()


def snapshot_loading() -> bool:
    """True while the one cold full-load has not yet published a snapshot.
    Kicks that load on a background daemon thread (single-flighted on
    `_check_lock`), so a caller can answer its request immediately and poll
    instead of holding a request thread for the whole load. Once the snapshot
    is published this is False forever — readers then serve from memory."""
    global _loaded, _last_check
    if _loaded:
        return False
    if _check_lock.acquire(blocking=False):
        def run() -> None:
            global _loaded, _last_check
            try:
                _freshen(full=True)
                _loaded = True
                _last_check = time.monotonic()
            except Exception:
                pass  # keep unloaded; the next call retries
            finally:
                _check_lock.release()

        threading.Thread(target=run, daemon=True, name="store-cold-load").start()
    return True


def prs() -> dict[int, Pr]:
    _ensure()
    return _prs


def generation() -> int:
    """Monotonic identity of the published snapshot: advances whenever a freshen
    publishes a change, and always on an explicit `refresh()`. Two equal reads
    mean the snapshot (PRs, clusters, author table) is the same; callers key
    snapshot-derived caches on it. Reads the counter as-is — `prs()` is what
    triggers loading."""
    return _generation


def clusters() -> dict[int, Cluster]:
    _ensure()
    return _clusters


def pr_to_clusters() -> dict[int, list[int]]:
    """Map each PR to the sorted list of clusters it belongs to (#196)."""
    _ensure()
    return _pr_to_clusters_idx


def pr_body(n: int) -> str | None:
    """The PR description, omitted from the snapshot's bulk load and read from the
    store on demand (detail view). None when never ingested — the caller falls
    back to a live GitHub fetch."""
    return _store.pr_bodies([int(n)]).get(int(n))


def pr_bodies(ns: list[int]) -> dict[int, str | None]:
    """Stored PR descriptions for `ns`, read from the store on demand (deep
    search hydrates its candidate set, which `prs()` carries body-free)."""
    return _store.pr_bodies(ns)


def author_stats(handle: str | None) -> dict | None:
    """Per-author leaderboard stats for `handle` (store-derived, #453): the live
    snapshot group-by folded with the historical baseline, computed once per
    snapshot and memoized. None for a falsy handle or an author with no PRs on
    either side."""
    if not handle:
        return None
    _ensure()
    global _author_table, _author_baseline
    if _author_table is None:
        if _author_baseline is None:
            _author_baseline = _store.load_author_baseline().get("authors") or {}
        _author_table = authors.author_stats(_author_baseline, _prs)
    return _author_table.get(handle.lower())


def runs(limit: int | None = None, since: str | None = None) -> list[storekit.RunRecord]:
    """Run-ledger records from the store, typed (PhaseRun | StoreEdit), in
    insertion order — all of them when `limit` is omitted, else only the last
    `limit`. `since` filters to records at or after that ISO instant (the
    ledger's indexed `ts` column) and composes with `limit`."""
    return _store.runs(limit=limit, since=since)


def action_items() -> list[dict]:
    """Operator/first-party worklist items (rotate-secret, salvage-fix, …).
    Not cached — small, and status changes need to be read back live."""
    return _store.load_action_items().get("items", [])


def set_action_item_status(item_id: str, status: str) -> dict:
    from pipeline import actions  # pipeline policy (path set up at import)
    reg = _store.load_action_items()
    actions.set_status(reg, item_id, status)
    _store.save_action_items(reg)
    return next(i for i in reg["items"] if i["id"] == item_id)


def safety_by_pr() -> dict[int, dict]:
    """Compat shim for modules that key off the old safety verdict shape
    ({verdict, findings, override}). Backed by the store's security section."""
    out: dict[int, dict] = {}
    for n, rec in prs().items():
        sec = rec.section("security")
        if sec:
            out[n] = sec
    return out


def refresh() -> None:
    """Freshen the snapshot synchronously now — used after a local pipeline phase,
    an executor action, or the live sweep changes the store, so the next read
    reflects it immediately instead of waiting for the debounced background check.
    Incremental (it picks up rows written since the watermark), so it's cheap to
    call on an action path; a cold snapshot still loads
    in full because the watermark starts None. Cluster removals arrive as tombstones
    on the same watermark, so they drop here too."""
    global _loaded, _last_check, _author_baseline, _author_table, _generation
    with _check_lock:
        # Re-read the baseline registry (it may have been recaptured) and drop the
        # leaderboard folded from it, even when the PR snapshot itself is unchanged.
        _author_baseline = None
        _author_table = None
        _freshen()
        # An explicit refresh means "make the next read reflect now", so bump the
        # snapshot identity even when the store rows are unchanged — the reloaded
        # author baseline (and anything else keyed on generation()) must be
        # recomputed.
        _generation += 1
        _loaded = True
        _last_check = time.monotonic()
