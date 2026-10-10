"""Cached read-side access for the Issue app.

The default Issues table uses a light snapshot with candidate PR arrays omitted,
then hydrates only the visible page with full rows. Duplicate triage can opt into
the full issue cache lazily. The first load starts from this machine's on-disk
copy of the snapshot (`snapshot_cache`) and reads only what changed since it;
the runs ledger starts from its own copy the same way.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from issue_triage.issue_store import IssueStore
from pipeline import storekit
from prospector_app.backend import run_ledger
from prospector_app.backend import snapshot_cache
from prospector_app.backend.snapshot import LazySnapshot

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue, IssueCluster

STORE_ROOT: Path | None = None
CHECK_DEBOUNCE = 10.0
# How often a freshen that changed the snapshot rewrites the disk copy.
CACHE_EVERY = 600.0
CACHE_NAME = "issues"
LEDGER_CACHE_NAME = "issue-runs"


@dataclass
class _IssueSnapshotState:
    store: IssueStore | None = None
    issues: dict[int, Issue] = field(default_factory=dict)
    clusters: dict[int, IssueCluster] = field(default_factory=dict)
    issue_watermark: str | None = None
    cluster_watermark: str | None = None
    full_issues: dict[int, Issue] | None = None
    full_key: tuple[str | None, str | None] | None = None
    runs: list[storekit.RunRecord] = field(default_factory=list)
    runs_ledger: run_ledger.RunLedger[storekit.RunRecord] | None = None
    generation: int = 0
    cache_written: float = 0.0
    cache_generation: int | None = None

    def reset(self) -> None:
        self.store = None
        self.issues = {}
        self.clusters = {}
        self.issue_watermark = None
        self.cluster_watermark = None
        self.full_issues = None
        self.full_key = None
        self.runs = []
        self.runs_ledger = None
        self.generation += 1
        self.cache_generation = None

    def invalidate_full(self) -> None:
        self.full_issues = None
        self.full_key = None


_state = _IssueSnapshotState()


def set_store_root(root: Path | str | None) -> None:
    global STORE_ROOT
    normalized = Path(root) if root is not None else None
    if normalized == STORE_ROOT:
        return
    STORE_ROOT = normalized
    _state.reset()
    _snapshot.invalidate()
    _runs_snapshot.invalidate()


def store() -> IssueStore:
    if _state.store is None:
        _state.store = IssueStore(STORE_ROOT)
    return _state.store


def _store_key(st: IssueStore) -> str:
    return st.engine.url.render_as_string(hide_password=True)


def _freshen(full: bool = False) -> None:
    st = store()
    if full:
        copy = snapshot_cache.load(CACHE_NAME, _store_key(st))
        if copy is not None:
            live = st.issue_ids()
            _state.issues = st.issue_views(
                {n: rec for n, rec in copy.records.items() if n in live})
            _state.issue_watermark = copy.watermark
            _state.clusters = {}
            _state.cluster_watermark = None
            _state.invalidate_full()
            _state.generation += 1
            _freshen(False)
            return
    issue_delta, issue_hi = st.issues_since(
        None if full else _state.issue_watermark, omit_candidates=True)
    cluster_delta, cluster_hi = st.issue_clusters_since(None if full else _state.cluster_watermark)
    _state.issues = dict(issue_delta) if full else {**_state.issues, **issue_delta}
    _state.clusters = dict(cluster_delta) if full else {**_state.clusters, **cluster_delta}
    if issue_hi:
        _state.issue_watermark = (
            issue_hi if (full or _state.issue_watermark is None)
            else max(_state.issue_watermark, issue_hi)
        )
    if cluster_hi:
        _state.cluster_watermark = (
            cluster_hi if (full or _state.cluster_watermark is None)
            else max(_state.cluster_watermark, cluster_hi)
        )
    if issue_delta or cluster_delta:
        _state.invalidate_full()
    if full or issue_delta or cluster_delta:
        _state.generation += 1
    _write_cache(st, force=full)


def _write_cache(st: IssueStore, *, force: bool) -> None:
    """Write the disk copy when the snapshot moved since the last one, at most
    once per CACHE_EVERY unless `force`."""
    if _state.cache_generation == _state.generation:
        return
    if not force and time.monotonic() - _state.cache_written < CACHE_EVERY:
        return
    if snapshot_cache.save(CACHE_NAME, _store_key(st), _state.issue_watermark,
                           st.issue_records(_state.issues)):
        _state.cache_written = time.monotonic()
        _state.cache_generation = _state.generation


def loading() -> bool:
    """True until the first load has published a snapshot."""
    return not _snapshot.loaded


_snapshot = LazySnapshot(_freshen, debounce=CHECK_DEBOUNCE)


def _freshen_runs(full: bool) -> None:
    """Bring the issue runs ledger current, reading only the rows the
    in-memory copy lacks (`run_ledger.RunLedger`, started from its disk copy);
    `full` is irrelevant."""
    st = store()
    if _state.runs_ledger is None or _state.runs_ledger.source is not st:
        _state.runs_ledger = run_ledger.RunLedger(
            st, snapshot_cache.runs_file(LEDGER_CACHE_NAME, _store_key(st)))
    _state.runs = [r.record for r in _state.runs_ledger.rows()]


_runs_snapshot = LazySnapshot(_freshen_runs, debounce=CHECK_DEBOUNCE)


def issues() -> dict[int, Issue]:
    _snapshot.ensure()
    return _state.issues


def clusters() -> dict[int, IssueCluster]:
    _snapshot.ensure()
    return _state.clusters


def watermarks() -> tuple[str | None, str | None]:
    _snapshot.ensure()
    return _state.issue_watermark, _state.cluster_watermark


def generation() -> int:
    """Identity of the published issue snapshot, including store-root resets."""
    _snapshot.ensure()
    return _state.generation


def runs() -> list[storekit.RunRecord]:
    """The issue pipeline's runs ledger, typed, oldest first — served from a
    debounced snapshot, re-read from the store at most once per CHECK_DEBOUNCE
    seconds."""
    _runs_snapshot.ensure()
    return _state.runs


def refresh() -> None:
    _snapshot.refresh()
    _runs_snapshot.refresh()


def load_full_issues(ns: list[int]) -> dict[int, Issue]:
    return store().load_issues(ns)


def full_issues() -> dict[int, Issue]:
    key = watermarks()
    if _state.full_issues is None or _state.full_key != key:
        _state.full_issues = store().all_issues()
        _state.full_key = key
    return _state.full_issues
