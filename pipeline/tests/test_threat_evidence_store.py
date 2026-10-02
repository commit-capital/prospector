"""The threat_evidence table: insert-only rows, metadata reads without blobs."""
from __future__ import annotations

import pytest

from pipeline import schema
from pipeline.store import Store
from pipeline.storekit import ValidationError


def _append(store: Store, pr: int = 7, head: str = "h1", *, author: str | None = "mallory",
            at: str = "2026-10-02T17:00:00+00:00", complete: bool = True) -> int:
    return store.append_threat_evidence(
        pr=pr, head_sha=head, author=author, captured_at=at, complete=complete,
        data={"pr": pr, "head_sha": head}, diff_gz=b"\x1f\x8bdiff", prior_gz=None)


def test_append_returns_id_and_reads_back_without_blobs(tmp_path):
    store = Store(tmp_path)
    cid = _append(store)
    [rec] = store.threat_evidence(pr=7)
    assert rec.id == cid and rec.head_sha == "h1" and rec.complete is True
    assert rec.data == {"pr": 7, "head_sha": "h1"}
    assert store.threat_evidence_record(cid) == rec
    blobs = store.threat_evidence_blobs(cid)
    assert blobs is not None and blobs.diff_gz == b"\x1f\x8bdiff" and blobs.prior_gz is None


def test_listing_is_newest_first_and_filters(tmp_path):
    store = Store(tmp_path)
    _append(store, 7, "a", at="2026-10-01T00:00:00+00:00")
    _append(store, 7, "b", at="2026-10-02T00:00:00+00:00")
    _append(store, 8, "c", author="eve")
    assert [r.head_sha for r in store.threat_evidence(pr=7)] == ["b", "a"]
    assert [r.pr for r in store.threat_evidence(author="eve")] == [8]
    assert len(store.threat_evidence()) == 3


def test_record_and_blobs_for_unknown_id_are_none(tmp_path):
    store = Store(tmp_path)
    assert store.threat_evidence_record(99) is None
    assert store.threat_evidence_blobs(99) is None


def test_append_validates(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValidationError):
        store.append_threat_evidence(pr=7, head_sha="", author=None, captured_at="x",
                                     complete=False, data={}, diff_gz=None, prior_gz=None)
    with pytest.raises(ValidationError):
        store.append_threat_evidence(pr=7, head_sha="h", author=None, captured_at="x",
                                     complete=False, data={}, diff_gz="text",  # type: ignore[arg-type]
                                     prior_gz=None)


def test_store_has_no_update_or_delete_for_evidence():
    names = [n for n in dir(Store) if "evidence" in n and not n.startswith("_")]
    assert sorted(names) == ["append_threat_evidence", "threat_evidence",
                             "threat_evidence_blobs", "threat_evidence_heads",
                             "threat_evidence_record"]


def test_schema_version_is_30():
    assert schema.STORE_SCHEMA_VERSION == 30
