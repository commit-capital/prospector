from __future__ import annotations

import json

import pytest

from pipeline import storekit
from pipeline.evals import eval_set, fixer_eval


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append_run(self, record: dict) -> None:
        storekit.parse_run(record)
        self.rows.append(record)

    def runs(self, limit: int | None = None, since: str | None = None,
             phase: str | None = None) -> list[storekit.RunRecord]:
        rows = [r for r in self.rows if phase is None or r.get("phase") == phase]
        return [storekit.parse_run(r) for r in (rows if limit is None else rows[-limit:])]


SHA, MODELS = "s" * 16, "opus,sonnet,opus"


def _sample(store: FakeStore, case: str, *, ending: str = "fixed", oracle: bool | None = True,
            sha: str = SHA, craft: str | None = "win", judge: str | None = None) -> dict:
    stats = {"case": case, "issue": int(case.split("-")[1]) if "-" in case else int(case),
             "ending": ending, "oracle_pass": oracle, "our_fix": f"fix {case}",
             "fixer": {"prompt_sha": sha, "models": MODELS},
             "craft": {"result": craft, "judge_sha": judge or fixer_eval.craft_judge.JUDGE_SHA}}
    store.append_run({"phase": "replay:instance", "started": "t", "finished": "t",
                      "trigger": "cli", "stats": stats})
    return stats


def test_samples_skip_faults_and_other_configurations():
    store = FakeStore()
    _sample(store, "1")
    _sample(store, "1", ending="sandbox")
    _sample(store, "2", sha="other")
    _sample(store, "2", ending="no-fix", oracle=False, craft=None)
    got = fixer_eval.samples(store, SHA, MODELS)  # type: ignore[arg-type]
    assert {k: [r["ending"] for r in v] for k, v in got.items()} == {"1": ["fixed"],
                                                                    "2": ["no-fix"]}


def _base(sha: str, *issues: int) -> dict:
    return {"base_sha": sha, "instances": [{"issue": n, "pr": n, "verdict": "fair"}
                                           for n in issues]}


def _case(issue: int, oracle: str = "craft-only") -> eval_set.Case:
    return eval_set.Case(case=f"rival-{issue}-{issue + 1}", source="rival", issue=issue,
                         pr=issue + 1, merge_sha="", base_sha="p", oracle=oracle,  # type: ignore[arg-type]
                         detail="", report_title="t", report_body="b", landed_diff="",
                         test_files=[], nontest_files=[])


def test_the_gate_set_takes_every_case_then_spreads_history_across_bases():
    manifest = {"bases": [_base("a", 5, 1, 9), _base("b", 4, 2), _base("c", 7)]}
    cases = [_case(10), _case(30), _case(20, oracle="unusable")]
    assert fixer_eval.choose_gate_set(manifest, cases, size=6) == [
        "rival-30-31", "rival-10-11", "1", "2", "7", "5"]


def test_a_gate_set_case_no_longer_usable_is_dropped():
    manifest = {"bases": [_base("a", 1)]}
    keep, dropped = fixer_eval.usable_gate_set(
        ["1", "3", "rival-10-11", "rival-20-21"], manifest,
        [_case(10), _case(20, oracle="unusable")])
    assert keep == ["1", "rival-10-11"] and dropped == ["3", "rival-20-21"]


def _never(case, rec):
    raise AssertionError("re-judged")


def test_a_sample_under_today_s_judge_is_read_as_is():
    store = FakeStore()
    rec = _sample(store, "1", craft="tie")
    got = fixer_eval.craft_of(store, "1", rec, cache={}, rejudge=_never)  # type: ignore[arg-type]
    assert got is not None and got["result"] == "tie"


def test_an_older_judgment_is_rejudged_once_and_cached():
    store = FakeStore()
    rec = _sample(store, "1", craft="win", judge="old")
    calls = []

    def rejudge(case, r):
        calls.append(case)
        return {"result": "loss", "points": [3], "judge_sha": fixer_eval.craft_judge.JUDGE_SHA}
    got = fixer_eval.craft_of(store, "1", rec, cache=fixer_eval.load_craft_cache(store),  # type: ignore[arg-type]
                              rejudge=rejudge)
    assert got == {"result": "loss", "points": [3],
                   "judge_sha": fixer_eval.craft_judge.JUDGE_SHA}
    again = fixer_eval.craft_of(store, "1", rec, cache=fixer_eval.load_craft_cache(store),  # type: ignore[arg-type]
                                rejudge=_never)
    assert again == got and calls == ["1"]


def test_a_run_with_no_fix_has_no_craft():
    store = FakeStore()
    rec = _sample(store, "1", ending="no-fix", oracle=False, craft=None, judge="old")
    assert fixer_eval.craft_of(store, "1", rec, cache={}, rejudge=_never) is None  # type: ignore[arg-type]


# --- the gate -----------------------------------------------------------------

PIN_SHA = "p" * 16


@pytest.fixture
def gating(tmp_path, monkeypatch):
    """A pin over a gate set of history bugs "1".."n" (all fair on one base);
    the test fills each side's samples through `side["pinned"|"candidate"]`, a
    case -> [(oracle passed, craft result)] map the injected top-ups record."""
    store = FakeStore()
    cand = fixer_eval.config()
    knobs: dict = {"n": 4, "pinned": {}, "candidate": {}, "topped": {"pinned": [], "candidate": []}}

    def setup():
        ids = [str(i) for i in range(1, knobs["n"] + 1)]
        path = tmp_path / "pins.json"
        path.write_text(json.dumps({"fixer": {"prompt_sha": PIN_SHA, "model": MODELS,
                                              "gate_set": ids, "provisional": True,
                                              "score": None, "run": None}}))
        monkeypatch.setattr(eval_set, "load_manifest", lambda s=None: {
            "bases": [{"base_sha": "a", "instances": [
                {"issue": int(i), "pr": int(i), "verdict": "fair"} for i in ids]}],
            "fair": len(ids), "stale": []})
        monkeypatch.setattr(eval_set, "load_cases", lambda s=None: [])
        return path

    def top_up(side, sha):
        def run(missing):
            knobs["topped"][side].append(sorted(missing))
            for case in missing:
                for ok, craft in knobs[side].get(case, []):
                    _sample(store, case, sha=sha, oracle=ok, craft=craft,
                            ending="fixed" if craft else "no-fix")
        return run

    def gate():
        return fixer_eval.gate(store, pins_path=setup(),  # type: ignore[arg-type]
                               top_up_pinned=top_up("pinned", PIN_SHA),
                               top_up_candidate=top_up("candidate", cand["prompt_sha"]),
                               rejudge=_never)
    knobs["gate"] = gate
    knobs["store"] = store
    return knobs


def _all(n: int, ok: bool, craft: str | None) -> dict:
    return {str(i): [(ok, craft), (ok, craft)] for i in range(1, n + 1)}


def test_a_candidate_that_loses_an_oracle_case_the_pin_held_is_refused(gating):
    gating["n"] = 13
    gating["pinned"] = _all(13, True, "win")
    gating["candidate"] = {**_all(13, True, "win"), "3": [(False, None), (False, None)]}
    result = gating["gate"]()
    assert not result.passed and not result.provisional
    assert any("3" in r and "oracle" in r for r in result.reasons)


def test_a_candidate_a_case_s_worth_lower_on_craft_still_passes(gating):
    gating["n"] = 12
    gating["pinned"] = _all(12, True, "win")
    gating["candidate"] = {**_all(12, True, "win"), "5": [(True, "loss"), (True, "loss")]}
    result = gating["gate"]()
    assert result.passed and result.candidate.craft_rate == round(11 / 12, 3)


def test_a_candidate_two_cases_lower_on_craft_is_refused(gating):
    gating["n"] = 12
    gating["pinned"] = _all(12, True, "win")
    gating["candidate"] = {**_all(12, True, "win"), "5": [(True, "loss"), (True, "loss")],
                           "6": [(True, "loss"), (True, "loss")]}
    result = gating["gate"]()
    assert not result.passed and any("craft" in r for r in result.reasons)
    assert result.candidate.losses_by_point == {}


def test_below_twelve_judged_cases_the_gate_is_provisional_and_passes(gating):
    gating["pinned"] = _all(4, True, "win")
    gating["candidate"] = _all(4, False, None)
    result = gating["gate"]()
    assert result.passed and result.provisional and result.reasons


def test_the_gate_tops_up_only_cases_short_of_two_samples(gating):
    gating["pinned"] = _all(4, True, "win")
    gating["candidate"] = _all(4, True, "win")
    cand = fixer_eval.config()["prompt_sha"]
    for case in ("1", "2"):
        _sample(gating["store"], case, sha=cand)
        _sample(gating["store"], case, sha=cand)
    gating["gate"]()
    assert gating["topped"]["candidate"] == [["3", "4"]]
    assert gating["topped"]["pinned"] == [["1", "2", "3", "4"]]


def test_the_candidate_runs_on_the_code_s_default_models(monkeypatch):
    seen = {}
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_MODELS", "haiku")

    def run(**kw):
        import os
        seen["models"] = os.environ["TRIAGE_ISSUE_FIX_MODELS"]
        seen["only"] = kw["only"]
        return 0
    monkeypatch.setattr(eval_set, "run", run)
    fixer_eval.run_candidate(["1"])
    assert seen == {"models": "opus,sonnet,opus", "only": {"1"}}
    import os
    assert os.environ["TRIAGE_ISSUE_FIX_MODELS"] == "haiku"


def test_a_failed_pinned_run_stops_the_gate(monkeypatch, tmp_path):
    import subprocess
    from contextlib import contextmanager
    from pipeline.evals import reviewer_eval

    @contextmanager
    def checkout(commit):
        yield tmp_path
    monkeypatch.setattr(reviewer_eval, "pinned_checkout", checkout)
    monkeypatch.setattr(reviewer_eval, "pinned_commit", lambda key, pin: "c" * 40)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout="", stderr="boom: no base"))
    store = FakeStore()
    store.engine = type("E", (), {"url": type("U", (), {  # type: ignore[attr-defined]
        "render_as_string": lambda self, hide_password: "sqlite://"})()})()
    with pytest.raises(RuntimeError, match="boom: no base"):
        fixer_eval.run_pinned({"prompt_sha": PIN_SHA, "model": MODELS}, store, ["1"])  # type: ignore[arg-type]
