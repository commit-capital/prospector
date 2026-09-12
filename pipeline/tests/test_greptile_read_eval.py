"""Greptile severity dataset and scorer."""

from __future__ import annotations

from pipeline.evals import greptile_read


def test_defects_precision_recall() -> None:
    labels = [
        {"pr": 1, "expected_severity": "defects"},
        {"pr": 2, "expected_severity": "nits"},
        {"pr": 3, "expected_severity": "defects"},
    ]
    preds = {1: "defects", 2: "defects", 3: "nits"}  # 1 TP, 1 FP, 1 FN
    r = greptile_read.score(labels, preds)
    assert r["defects_precision"] == 0.5  # 1 TP / (1 TP + 1 FP)
    assert r["defects_recall"] == 0.5  # 1 TP / (1 TP + 1 FN)


def test_golden_labels_are_valid() -> None:
    labels = greptile_read.load_labels()
    assert labels
    assert len({row["pr"] for row in labels}) == len(labels)
