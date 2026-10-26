"""The fixer gate (docs/superpowers/specs/2026-10-24-fixer-ratchet-design.md):
score the production cross lane under its pinned configuration and under the
code's, on a fixed gate set of eval-set cases, by the hidden oracle and by
craft against the human fix.

The `fixer` pin shares the reviewer pins file. Samples are the
`replay:instance` records `eval_set.run` writes, keyed by case and fixer
configuration.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from itertools import zip_longest
from pathlib import Path

from pipeline import diffpaths, profile, settings, storekit
from pipeline.evals import craft_judge, eval_set, reviewer_eval
from pipeline.evals import issue_fix_replay as replay
from pipeline.store import Store

PIN = "fixer"
CRAFT_PHASE = "fixer-eval:craft"

# A stored fix's craft under a judge: (case, fix hash, judge_sha) -> Craft dict or None.
CraftCache = dict[tuple[str, str, str], dict | None]
Rejudge = Callable[[str, dict], dict | None]


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


def _fix_key(rec: dict) -> str:
    return hashlib.sha256(str(rec.get("our_fix") or "").encode()).hexdigest()[:16]


def load_craft_cache(store: Store) -> CraftCache:
    cache: CraftCache = {}
    for rec in store.runs(limit=eval_set._LEDGER_SCAN, phase=CRAFT_PHASE):
        stats = rec.raw.get("stats") or {}
        cache[(stats["case"], stats["fix"], stats["judge_sha"])] = stats.get("craft")
    return cache


def craft_of(store: Store, case: str, rec: dict, *, cache: CraftCache,
             rejudge: Rejudge) -> dict | None:
    """A sample's craft under today's judge (`craft_judge.JUDGE_SHA`), as a
    `Craft` dict with a result, or None when the run has no fix or no verdict.
    A judgment by another judge is re-judged from the stored fix once, and the
    result kept as a `fixer-eval:craft` row."""
    if rec.get("ending") != "fixed":
        return None
    craft = rec.get("craft") or {}
    if craft.get("judge_sha") != craft_judge.JUDGE_SHA:
        key = (case, _fix_key(rec), craft_judge.JUDGE_SHA)
        if key not in cache:
            cache[key] = rejudge(case, rec)
            now = storekit.now()
            store.append_run({"phase": CRAFT_PHASE, "started": now, "finished": now,
                              "trigger": "cli",
                              "stats": {"case": case, "fix": key[1], "judge_sha": key[2],
                                        "craft": cache[key]}})
        craft = cache[key] or {}
    return craft if craft.get("result") in ("win", "tie", "loss") else None


def rejudger(jobs: dict[str, eval_set._Job]) -> Rejudge:
    """Judge a stored fix of a case in `jobs` against its reference, over the
    tree the fix was made on."""
    def rejudge(case: str, rec: dict) -> dict | None:
        job = jobs[case]
        base = eval_set._held_base(job.base_sha)
        pre_patch = replay.transform_to_p(base.clone, job.instance.pre_fix, base.sha,
                                          profile.active())
        reference = diffpaths.filter_diff(job.instance.landed_diff,
                                          lambda p: not diffpaths.is_test_path(p))
        return craft_judge.score_craft(base.clone, pre_patch, str(rec.get("our_fix") or ""),
                                       reference, title=job.instance.report_title,
                                       body=job.instance.report_body)["craft"]
    return rejudge


if __name__ == "__main__":
    init_pin()
