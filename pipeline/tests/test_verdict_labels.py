from __future__ import annotations

import pytest

from pipeline import verdict_labels, verdict_log
from pipeline.store import Store
from pipeline.verdict_labels import LabelRefused


def _row(store: Store, reviewer: str = "design", verdict: str = "pass",
         findings: list[dict] | None = None) -> int:
    vid = verdict_log.record(reviewer, verdict_log.Subject("pr", 7), verdict=verdict,
                             prompt_sha="p" * 16, model="opus",
                             data={"findings": findings or [{"title": "dup"}]},
                             inputs={}, store=store)
    assert vid is not None
    return vid


def test_label_stores_and_returns(tmp_path):
    store = Store(tmp_path)
    vid = _row(store)
    out = verdict_labels.label(store, vid, expected="fail", by="op", note="  missed dup  ",
                               findings={"0": "real"})
    assert out["expected"] == "fail" and out["note"] == "missed dup"
    assert out["findings"] == {"0": "real"} and out["by"] == "op" and out["source"] == "operator"
    rec = store.review_verdict(vid)
    assert rec is not None and rec.label == out


def test_failed_row_refused(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(LabelRefused, match="never judged"):
        verdict_labels.label(store, _row(store, verdict="failed"), expected="pass", by="op")


def test_expected_outside_vocabulary_refused(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(LabelRefused):
        verdict_labels.label(store, _row(store, "fixability", "go"), expected="GREEN", by="op")


def test_failure_value_is_not_an_expected_verdict(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(LabelRefused):
        verdict_labels.label(store, _row(store), expected="failed", by="op")


@pytest.mark.parametrize("findings", [{"0": "meh"}, {"3": "real"}, {"x": "real"}])
def test_bad_mark_or_index_refused(tmp_path, findings):
    store = Store(tmp_path)
    with pytest.raises(LabelRefused):
        verdict_labels.label(store, _row(store), expected="pass", by="op", findings=findings)


def test_security_marks_index_its_confirmed_findings(tmp_path):
    store = Store(tmp_path)
    vid = verdict_log.record("security", verdict_log.Subject("pr", 7), verdict="RED",
                             prompt_sha="p" * 16, model="opus",
                             data={"flagged": [{}, {}], "confirmed": [{"title": "x"}]},
                             inputs={}, store=store)
    assert vid is not None
    verdict_labels.label(store, vid, expected="RED", by="op", findings={"0": "real"})
    with pytest.raises(LabelRefused):
        verdict_labels.label(store, vid, expected="RED", by="op", findings={"1": "real"})


def test_relabel_replaces(tmp_path):
    store = Store(tmp_path)
    vid = _row(store)
    verdict_labels.label(store, vid, expected="fail", by="a")
    verdict_labels.label(store, vid, expected="pass", by="b", source="case-study")
    rec = store.review_verdict(vid)
    assert rec is not None and rec.label is not None
    assert (rec.label["expected"], rec.label["by"], rec.label["source"]) == (
        "pass", "b", "case-study")


def test_blank_operator_or_unknown_source_refused(tmp_path):
    store = Store(tmp_path)
    vid = _row(store)
    with pytest.raises(LabelRefused):
        verdict_labels.label(store, vid, expected="pass", by=" ")
    with pytest.raises(LabelRefused):
        verdict_labels.label(store, vid, expected="pass", by="op", source="robot")


def test_missing_row_is_key_error(tmp_path):
    with pytest.raises(KeyError):
        verdict_labels.label(Store(tmp_path), 999, expected="pass", by="op")


@pytest.mark.parametrize("reviewer,expected,verdict,call", [
    ("security", "RED", "YELLOW", "under"),
    ("fixability", "go", "skip", "over"),
    ("design", "fail", "fail", "right"),
])
def test_call_orders_by_severity(reviewer, expected, verdict, call):
    assert verdict_labels.call({"expected": expected}, verdict, reviewer) == call
