"""Scorer math only — no network, no golden file. See greptile_read_eval.score."""

from __future__ import annotations

from pipeline.greptile_read_eval import score


def test_defects_precision_recall() -> None:
    labels = [
        {"pr": 1, "expected_severity": "defects"},
        {"pr": 2, "expected_severity": "nits"},
        {"pr": 3, "expected_severity": "defects"},
    ]
    preds = {1: "defects", 2: "defects", 3: "nits"}  # 1 TP, 1 FP, 1 FN
    r = score(labels, preds)
    assert r["defects_precision"] == 0.5  # 1 TP / (1 TP + 1 FP)
    assert r["defects_recall"] == 0.5  # 1 TP / (1 TP + 1 FN)
