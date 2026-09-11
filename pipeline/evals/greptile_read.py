"""Score the GREPTILE READ agent against a hand-labeled golden set. Precision on
`defects` is the release gate: a false `defects` closes a good nitpick-only PR."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict

from pipeline.evals import datasets

DEFAULT_GOLDEN = Path(__file__).resolve().parent / "data" / "greptile_read.jsonl"
Severity = Literal["defects", "nits", "clean"]


class GoldenLabel(TypedDict):
    pr: int
    expected_severity: Severity


def load_labels(path: Path = DEFAULT_GOLDEN) -> list[GoldenLabel]:
    labels: list[GoldenLabel] = []
    for row in datasets.read_jsonl(path):
        pr = row.get("pr")
        severity = row.get("expected_severity")
        if not isinstance(pr, int):
            raise ValueError("every Greptile label needs an integer pr")
        if severity not in ("defects", "nits", "clean"):
            raise ValueError("every Greptile label needs a valid expected_severity")
        labels.append({"pr": pr, "expected_severity": severity})
    return labels


def score(
    labels: list[GoldenLabel], predictions: dict[int, str]
) -> dict[str, int | float]:
    tp = fp = fn = correct = 0
    for row in labels:
        want = row["expected_severity"]
        got = predictions.get(row["pr"])
        if got == want:
            correct += 1
        if got == "defects" and want == "defects":
            tp += 1
        elif got == "defects" and want != "defects":
            fp += 1
        elif got != "defects" and want == "defects":
            fn += 1
    return {
        "n": len(labels),
        "defects_precision": tp / (tp + fp) if (tp + fp) else 1.0,
        "defects_recall": tp / (tp + fn) if (tp + fn) else 1.0,
        "accuracy": correct / len(labels) if labels else 1.0,
    }
