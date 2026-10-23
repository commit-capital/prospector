"""The review_verdicts table: append-only rows, metadata reads without the inputs blob."""
from __future__ import annotations

import gzip

import pytest

from pipeline import schema
from pipeline.store import Store
from pipeline.storekit import ValidationError


def _append(store: Store, *, reviewer: str = "design", kind: str = "pr", subject: int = 7,
            at: str = "2026-10-23T12:00:00+00:00", verdict: str = "pass",
            inputs_gz: bytes | None = None, **over: object) -> int:
    fields: dict = {"at": at, "reviewer": reviewer, "subject_kind": kind, "subject": subject,
                    "head_sha": "h1", "base_sha": "b1", "prompt_sha": "p" * 16,
                    "model": "opus", "verdict": verdict, "data": {"findings": []},
                    "inputs_gz": inputs_gz, **over}
    return store.append_review_verdict(**fields)


def test_append_and_read_round_trip(tmp_path):
    store = Store(tmp_path)
    vid = _append(store)
    [rec] = store.review_verdicts(reviewer="design")
    assert rec.id == vid and rec.reviewer == "design" and rec.subject_kind == "pr"
    assert (rec.subject, rec.head_sha, rec.base_sha) == (7, "h1", "b1")
    assert (rec.prompt_sha, rec.model, rec.verdict) == ("p" * 16, "opus", "pass")
    assert rec.data == {"findings": []} and rec.label is None


def test_filters_and_order(tmp_path):
    store = Store(tmp_path)
    _append(store, at="2026-10-21T00:00:00+00:00")
    _append(store, reviewer="fixability", kind="issue", subject=9, verdict="go",
            at="2026-10-22T00:00:00+00:00")
    _append(store, subject=8, at="2026-10-23T00:00:00+00:00")
    assert [r.subject for r in store.review_verdicts()] == [8, 9, 7]
    assert [r.subject for r in store.review_verdicts(reviewer="design")] == [8, 7]
    assert [r.reviewer for r in store.review_verdicts(subject_kind="issue", subject=9)] == [
        "fixability"]
    assert [r.subject for r in store.review_verdicts(since="2026-10-22T00:00:00+00:00")] == [8, 9]


def test_inputs_blob_kept_out_of_records(tmp_path):
    store = Store(tmp_path)
    blob = gzip.compress(b'{"body":"x"}')
    vid = _append(store, inputs_gz=blob)
    [rec] = store.review_verdicts()
    assert not hasattr(rec, "inputs_gz")
    assert store.review_verdict_inputs(vid) == blob
    assert store.review_verdict_inputs(999) is None


@pytest.mark.parametrize("over", [
    {"kind": "cluster"}, {"subject": True}, {"data": []}, {"inputs_gz": "s"}, {"verdict": ""},
])
def test_validation(tmp_path, over):
    with pytest.raises(ValidationError):
        _append(Store(tmp_path), **over)


def test_the_schema_version_covers_review_verdicts():
    assert schema.STORE_SCHEMA_VERSION >= 38
