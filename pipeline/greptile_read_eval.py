"""Score the GREPTILE READ agent against a hand-labeled golden set. Precision on
`defects` is the release gate: a false `defects` closes a good nitpick-only PR."""

from __future__ import annotations


def score(labels: list[dict], predictions: dict[int, str]) -> dict:
    tp = fp = fn = correct = 0
    for row in labels:
        want = row["expected_severity"]
        got = predictions.get(int(row["pr"]))
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
