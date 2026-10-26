"""The fixer gate (docs/superpowers/specs/2026-10-24-fixer-ratchet-design.md):
score the production cross lane under its pinned configuration and under the
code's, on a fixed gate set of eval-set cases, by the hidden oracle and by
craft against the human fix.

The `fixer` pin shares the reviewer pins file. Samples are the
`replay:instance` records `eval_set.run` writes, keyed by case and fixer
configuration.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline import settings
from pipeline.evals import craft_judge, reviewer_eval

PIN = "fixer"


def config() -> dict[str, str]:
    """The fixer's prompts and default models as the code sets them, ignoring
    the environment: the configuration its pin is checked against."""
    return {"prompt_sha": craft_judge.fixer_config("cross")["prompt_sha"],
            "model": ",".join(settings.ISSUE_FIX_MODELS)}


def init_pin(path: Path = reviewer_eval.PINS) -> None:
    """Pin the fixer to the code's configuration, provisionally, unless pinned."""
    pins = json.loads(path.read_text()) if path.exists() else {}
    pins.setdefault(PIN, {**config(), "provisional": True, "gate_set": None, "score": None,
                          "run": None})
    path.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    init_pin()
