from __future__ import annotations

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
