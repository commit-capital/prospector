from __future__ import annotations

import gzip
import json

import pytest

from pipeline import verdict_log
from pipeline.store import Store
from pipeline.verdict_log import Subject


def _record(store: Store, subject: Subject | None = Subject("pr", 7), *,
            reviewer: str = "design", **over: object) -> int | None:
    fields: dict = {"verdict": "pass", "prompt_sha": "p" * 16, "model": "opus",
                    "data": {"findings": []}, "inputs": {"title": "Fix it"},
                    "head_sha": "h1", "store": store, **over}
    return verdict_log.record(reviewer, subject, **fields)


def test_no_subject_records_nothing(tmp_path):
    store = Store(tmp_path)
    assert _record(store, None) is None
    assert store.review_verdicts() == []


def test_records_row_with_gzipped_inputs(tmp_path):
    store = Store(tmp_path)
    vid = _record(store)
    assert vid is not None
    [rec] = store.review_verdicts()
    assert (rec.reviewer, rec.subject_kind, rec.subject, rec.verdict) == ("design", "pr", 7, "pass")
    blob = store.review_verdict_inputs(vid)
    assert blob is not None and json.loads(gzip.decompress(blob)) == {"title": "Fix it"}


def test_unknown_reviewer_or_verdict_raises(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValueError):
        _record(store, reviewer="nope")
    with pytest.raises(ValueError):
        _record(store, verdict="GREEN")


class _LockedStore:
    def append_review_verdict(self, **_: object) -> int:
        raise RuntimeError("locked")


def test_store_failure_never_raises(capsys):
    assert _record(_LockedStore()) is None  # type: ignore[arg-type]
    assert "could not record design on pr #7: locked" in capsys.readouterr().err


def test_prompt_sha_stable_and_sensitive():
    sha = verdict_log.prompt_sha("A __X__", "B")
    assert sha == verdict_log.prompt_sha("A __X__", "B") and len(sha) == 16
    assert sha != verdict_log.prompt_sha("A __Y__", "B")
    assert sha != verdict_log.prompt_sha("A __X__", "B", logic=1)
