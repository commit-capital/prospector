"""The fixer gate (docs/superpowers/specs/2026-10-24-fixer-ratchet-design.md):
score the production cross lane under its pinned configuration and under the
code's, on a fixed gate set of eval-set cases, by the hidden oracle and by
craft against the human fix.

The `fixer` pin shares the reviewer pins file. Samples are the
`replay:instance` records `eval_set.run` writes, keyed by case and fixer
configuration.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from itertools import zip_longest
from pathlib import Path

from pipeline import diffpaths, profile, settings, storekit
from pipeline.evals import craft_judge, eval_set, reviewer_eval
from pipeline.evals import issue_fix_replay as replay
from pipeline.store import Store

PIN = "fixer"
CRAFT_PHASE = "fixer-eval:craft"
RUN_PHASE = "fixer-eval:run"
# Lane runs per side per case; the gate is provisional below MIN_JUDGED cases
# craft-judged on both sides.
SAMPLES = 2
MIN_JUDGED = 12
_CRAFT_VALUE = {"win": 1.0, "tie": 0.5, "loss": 0.0}

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


@dataclass(frozen=True)
class FixerScore:
    cases: int
    oracle_cases: int
    oracle_rate: float | None
    judged: int
    craft_rate: float | None
    losses_by_point: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FixerGateResult:
    pinned: FixerScore
    candidate: FixerScore
    passed: bool
    provisional: bool
    reasons: list[str]
    gate_set: list[str]
    dropped: list[str]
    unsampled: list[str]


@contextlib.contextmanager
def _models(models: str) -> Iterator[None]:
    """Run the cross lane's candidates on `models` for the duration."""
    before = os.environ.get("TRIAGE_ISSUE_FIX_MODELS")
    os.environ["TRIAGE_ISSUE_FIX_MODELS"] = models
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TRIAGE_ISSUE_FIX_MODELS", None)
        else:
            os.environ["TRIAGE_ISSUE_FIX_MODELS"] = before


def _run_args(prompt_sha: str, missing: list[str]) -> dict:
    return {"name": f"fixer-gate-{prompt_sha}", "passes": SAMPLES, "concurrency": 1,
            "refresh": False, "issues": None, "resume": True, "lane": "cross",
            "only": set(missing)}


def run_candidate(missing: list[str]) -> None:
    """Replay `missing` under this checkout's fixer, on the code's default models."""
    cand = config()
    with _models(cand["model"]):
        eval_set.run(**_run_args(cand["prompt_sha"], missing))


def run_pinned(pin: dict, store: Store, missing: list[str]) -> None:
    """Replay `missing` under the code main ran for `pin`, from a checkout of it,
    recording into this process's store."""
    commit = reviewer_eval.pinned_commit(PIN, pin)
    if commit is None:
        raise reviewer_eval.PinUnreplayable(
            f"no main commit pins the fixer to {pin['prompt_sha']}/{pin['model']}; "
            f"remove that pin and run --init")
    env = {**os.environ, "TRIAGE_SKIP_DOTENV": "1", "TRIAGE_ISSUE_FIX_MODELS": pin["model"],
           "TRIAGE_STORE_URL": store.engine.url.render_as_string(hide_password=False)}
    args = _run_args(pin["prompt_sha"], missing)
    with reviewer_eval.pinned_checkout(commit) as wt:
        done = subprocess.run(
            ["uv", "run", "--project", str(wt), "python", "-m", "pipeline.evals.eval_set", "run",
             "--name", args["name"], "--passes", str(SAMPLES), "--lane", "cross", "--resume",
             "--only", ",".join(missing)],
            cwd=wt, env=env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"the pinned fixer's run at {commit[:12]} failed: "
                           f"{done.stderr.strip()[-400:]}")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _score(runs: dict[str, list[dict]], crafts: dict[str, list[dict]], *,
           compared: list[str], oracle: list[str], judged: list[str]) -> FixerScore:
    oracle_rate = _mean([_mean([float(eval_set._correct(r)) for r in runs[c]]) or 0.0
                         for c in oracle])
    craft_rate = _mean([_mean([_CRAFT_VALUE[x["result"]] for x in crafts[c]]) or 0.0
                        for c in judged])
    points: dict[str, int] = {}
    for c in judged:
        for x in crafts[c]:
            if x["result"] != "win":
                for point in x.get("points") or []:
                    points[str(point)] = points.get(str(point), 0) + 1
    return FixerScore(
        cases=len(compared), oracle_cases=len(oracle),
        oracle_rate=None if oracle_rate is None else round(oracle_rate, 3),
        judged=len(judged), craft_rate=None if craft_rate is None else round(craft_rate, 3),
        losses_by_point=points)


def gate(store: Store, *, pins_path: Path = reviewer_eval.PINS, reset_gate_set: bool = False,
         top_up_pinned: Callable[[list[str]], None] | None = None,
         top_up_candidate: Callable[[list[str]], None] | None = None,
         rejudge: Rejudge | None = None) -> FixerGateResult:
    """Score the pinned fixer and this checkout's on the pin's gate set, topping
    each side up to SAMPLES runs per case, and both sides' craft under today's
    judge. The candidate passes when no oracle case the pin passed in every run
    fails in every one of its runs, its oracle rate is no more than one case's
    worth below the pin's, and so is its craft rate. Below MIN_JUDGED cases
    judged on both sides the gate is provisional and passes, reporting what it
    would have refused."""
    pin = json.loads(pins_path.read_text())[PIN]
    cand = config()
    manifest, cases = eval_set.load_manifest(store), eval_set.load_cases(store)
    chosen = (None if reset_gate_set else pin.get("gate_set")) or choose_gate_set(manifest,
                                                                                    cases)
    keep, dropped = usable_gate_set(chosen, manifest, cases)
    with_tests = {c.case for c in cases if c.oracle == "tests"}
    runs: dict[str, dict[str, list[dict]]] = {}
    for side, conf, top_up in (
            ("pinned", pin, top_up_pinned or (lambda m: run_pinned(pin, store, m))),
            ("candidate", cand, top_up_candidate or run_candidate)):
        have = samples(store, conf["prompt_sha"], conf["model"])
        missing = [c for c in keep if len(have.get(c, [])) < SAMPLES]
        if missing:
            top_up(missing)
            have = samples(store, conf["prompt_sha"], conf["model"])
        runs[side] = {c: have[c] for c in keep if have.get(c)}
    compared = [c for c in keep if c in runs["pinned"] and c in runs["candidate"]]

    jobs: dict[str, eval_set._Job] = {}

    def planned_rejudge(case: str, rec: dict) -> dict | None:
        if not jobs:  # the replay inputs are assembled once, when first needed
            jobs.update({j.case: j for j in eval_set.plan_jobs(
                store, manifest, refresh=False, issues=None, only=set(keep), cases=True)})
        return rejudger(jobs)(case, rec)
    cache = load_craft_cache(store)
    crafts = {side: {c: [x for r in runs[side][c]
                         if (x := craft_of(store, c, r, cache=cache,
                                           rejudge=rejudge or planned_rejudge))]
                     for c in compared} for side in runs}
    oracle = [c for c in compared if c.isdigit() or c in with_tests]
    judged = [c for c in compared if crafts["pinned"][c] and crafts["candidate"][c]]
    scores = {side: _score(runs[side], crafts[side], compared=compared, oracle=oracle,
                           judged=judged) for side in runs}
    reasons = []
    for c in oracle:
        p, q = runs["pinned"][c], runs["candidate"][c]
        if (len(p) >= SAMPLES and all(map(eval_set._correct, p))
                and len(q) >= SAMPLES and not any(map(eval_set._correct, q))):
            reasons.append(f"case {c}: the pin passed the oracle in every run; "
                           f"the candidate failed it in every run")
    for measure, n in (("oracle_rate", len(oracle)), ("craft_rate", len(judged))):
        p_rate, c_rate = getattr(scores["pinned"], measure), getattr(scores["candidate"], measure)
        if n and p_rate is not None and c_rate is not None and c_rate < p_rate - 1 / n - 1e-3:
            reasons.append(f"{measure.replace('_', ' ')} {c_rate} against the pin's {p_rate}, "
                           f"more than one case's worth lower")
    provisional = len(judged) < MIN_JUDGED
    return FixerGateResult(pinned=scores["pinned"], candidate=scores["candidate"],
                           passed=provisional or not reasons, provisional=provisional,
                           reasons=reasons, gate_set=keep, dropped=dropped,
                           unsampled=[c for c in keep if c not in compared])


def write_pin(result: FixerGateResult, pins_path: Path = reviewer_eval.PINS) -> None:
    pins = json.loads(pins_path.read_text())
    pins[PIN] = {**config(), "provisional": result.provisional, "gate_set": result.gate_set,
                 "score": asdict(result.candidate), "run": storekit.now()}
    pins_path.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n")


def _summary(result: FixerGateResult) -> str:
    def line(name: str, s: FixerScore) -> str:
        return (f"  {name}: oracle {s.oracle_rate} over {s.oracle_cases}, craft {s.craft_rate} "
                f"over {s.judged} judged" + (f", losses by point {s.losses_by_point}"
                                             if s.losses_by_point else ""))
    verdict = "PASS" if result.passed else "REFUSED"
    lines = [f"fixer: {verdict}" + (" (provisional)" if result.provisional else ""),
             line("pinned", result.pinned), line("candidate", result.candidate)]
    lines += [f"  - {r}" for r in result.reasons]
    if result.dropped:
        lines.append(f"  dropped, no longer runnable: {', '.join(result.dropped)}")
    if result.unsampled:
        lines.append(f"  without samples on both sides: {', '.join(result.unsampled)}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.evals.fixer_eval")
    ap.add_argument("--init", action="store_true", help="pin the fixer, provisionally, if unpinned")
    ap.add_argument("--live", action="store_true", help="run the gate, calling agents")
    ap.add_argument("--reset-gate-set", action="store_true",
                    help="choose the gate set afresh instead of the pin's")
    args = ap.parse_args(argv)
    if args.init:
        init_pin()
        return 0
    if not args.live:
        ap.error("give --init or --live")
    store = Store()
    started = storekit.now()
    result = gate(store, reset_gate_set=args.reset_gate_set)
    store.append_run({"phase": RUN_PHASE, "started": started, "finished": storekit.now(),
                      "trigger": "cli", "stats": asdict(result)})
    print(_summary(result))
    if result.passed:
        write_pin(result)
        print(f"re-pinned the fixer; commit "
              f"{reviewer_eval.PINS.relative_to(reviewer_eval.REPO_ROOT)}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
