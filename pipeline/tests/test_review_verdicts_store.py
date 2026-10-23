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


def test_set_label_round_trip(tmp_path):
    store = Store(tmp_path)
    vid = _append(store)
    assert store.set_review_verdict_label(vid, {"expected": "fail", "by": "op"}) is True
    rec = store.review_verdict(vid)
    assert rec is not None and rec.label == {"expected": "fail", "by": "op"}
    assert store.review_verdicts()[0].label == {"expected": "fail", "by": "op"}


def test_set_label_on_missing_id_is_false(tmp_path):
    assert Store(tmp_path).set_review_verdict_label(999, {"expected": "pass"}) is False


def test_set_label_validates(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValidationError):
        store.set_review_verdict_label(_append(store), [])  # type: ignore[arg-type]


def test_single_row_read_missing_is_none(tmp_path):
    assert Store(tmp_path).review_verdict(999) is None


def test_labeled_filter(tmp_path):
    store = Store(tmp_path)
    vid = _append(store)
    _append(store, subject=8)
    store.set_review_verdict_label(vid, {"expected": "fail"})
    assert [r.id for r in store.review_verdicts(labeled=True)] == [vid]
    assert [r.subject for r in store.review_verdicts(labeled=False)] == [8]


def test_clear_inputs_spares_labeled_and_recent(tmp_path):
    store = Store(tmp_path)
    blob = gzip.compress(b"{}")
    old = _append(store, at="2026-01-01T00:00:00+00:00", inputs_gz=blob)
    kept = _append(store, at="2026-01-01T00:00:00+00:00", inputs_gz=blob)
    store.set_review_verdict_label(kept, {"expected": "pass"})
    recent = _append(store, at="2026-10-01T00:00:00+00:00", inputs_gz=blob)
    assert store.clear_review_verdict_inputs("2026-06-01T00:00:00+00:00") == 1
    assert store.review_verdict_inputs(old) is None
    assert store.review_verdict_inputs(kept) == blob and store.review_verdict_inputs(recent) == blob
