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
from itertools import zip_longest
from pathlib import Path

from pipeline import settings
from pipeline.evals import craft_judge, eval_set, reviewer_eval
from pipeline.evals import issue_fix_replay as replay
from pipeline.store import Store

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


def samples(store: Store, prompt_sha: str, models: str) -> dict[str, list[dict]]:
    """The scored replay records of the configuration `prompt_sha`/`models`,
    by case id; a machine fault is no sample."""
    out: dict[str, list[dict]] = {}
    for rec in store.runs(limit=eval_set._LEDGER_SCAN, phase="replay:instance"):
        stats = rec.raw.get("stats") or {}
        fixer = stats.get("fixer") or {}
        if ((fixer.get("prompt_sha"), fixer.get("models")) == (prompt_sha, models)
                and not replay._is_fault(stats.get("ending"))):
            out.setdefault(eval_set._case_of(stats), []).append(stats)
    return out


def _history(manifest: dict) -> list[list[str]]:
    """Each base's fair bugs, lowest issue first, the bases in manifest order."""
    return [[str(n) for n in sorted(i["issue"] for i in b["instances"]
                                    if i["verdict"] == "fair")]
            for b in manifest["bases"]]


def choose_gate_set(manifest: dict, cases: list[eval_set.Case], size: int = 12) -> list[str]:
    """Every usable rival and case-study case, newest issue first, topped up
    with fair history bugs taken a base at a time across the bases."""
    picked = [c.case for c in sorted(cases, key=lambda c: -c.issue) if c.oracle != "unusable"]
    spread = [n for row in zip_longest(*_history(manifest)) for n in row if n is not None]
    return (picked + spread)[:size]


def usable_gate_set(gate_set: list[str], manifest: dict,
                    cases: list[eval_set.Case]) -> tuple[list[str], list[str]]:
    """`gate_set` split into the cases still runnable and those that are not."""
    runnable = {n for row in _history(manifest) for n in row} | {
        c.case for c in cases if c.oracle != "unusable"}
    return ([c for c in gate_set if c in runnable], [c for c in gate_set if c not in runnable])


if __name__ == "__main__":
    init_pin()
