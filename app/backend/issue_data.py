"""Cached read-side access for the Issue cockpit.

The default Issues table uses a light snapshot with candidate PR arrays omitted,
then hydrates only the visible page with full rows. Duplicate triage can opt into
the full issue cache lazily; nothing here runs at app startup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from issue_triage.issue_store import IssueStore
from pipeline import storekit
from app.backend.snapshot import LazySnapshot

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue, IssueCluster

STORE_ROOT: Path | None = None
CHECK_DEBOUNCE = 10.0


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

    def reset(self) -> None:
        self.store = None
        self.issues = {}
        self.clusters = {}
        self.issue_watermark = None
        self.cluster_watermark = None
        self.full_issues = None
        self.full_key = None
        self.runs = []

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


def _freshen(full: bool = False) -> None:
    st = store()
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


_snapshot = LazySnapshot(_freshen, debounce=CHECK_DEBOUNCE)


def _freshen_runs(full: bool) -> None:
    """Re-read the whole issue runs ledger (it has no watermark API, so every
    freshen is a full read and `full` is irrelevant)."""
    _state.runs = store().runs()


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


def runs() -> list[storekit.RunRecord]:
    """The issue pipeline's runs ledger, typed, oldest first — served from a
    debounced snapshot, re-read from the store at most once per CHECK_DEBOUNCE
    seconds."""
    _runs_snapshot.ensure()
    return _state.runs


def refresh() -> None:
    _snapshot.refresh()


def load_full_issues(ns: list[int]) -> dict[int, Issue]:
    return store().load_issues(ns)


def full_issues() -> dict[int, Issue]:
    key = watermarks()
    if _state.full_issues is None or _state.full_key != key:
        _state.full_issues = store().all_issues()
        _state.full_key = key
    return _state.full_issues
