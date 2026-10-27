from __future__ import annotations

import types

from pipeline import storekit
from pipeline.evals import craft_judge, fixer_eval, fixer_lessons

OURS = "diff --git a/src/q.ts b/src/q.ts\n--- a/src/q.ts\n+++ b/src/q.ts\n@@ -1 +1 @@\n-a\n+sql('WHERE x = 1')\n"
REF = "diff --git a/src/q.ts b/src/q.ts\n--- a/src/q.ts\n+++ b/src/q.ts\n@@ -1 +1 @@\n-a\n+x === 1\n"


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.verdicts: dict[int, list] = {}

    def append_run(self, record: dict) -> None:
        storekit.parse_run(record)
        self.rows.append(record)

    def runs(self, limit=None, since=None, phase=None):
        rows = [r for r in self.rows if phase is None or r.get("phase") == phase]
        return [storekit.parse_run(r) for r in rows]

    def review_verdicts(self, *, subject_kind=None, subject=None, labeled=None, **kw):
        return self.verdicts.get(subject, [])


class FakeIssues:
    def __init__(self, choices: dict[int, dict]) -> None:
        self.choices = choices

    def issue_rows(self, paths, **kw):
        return [({"issue": n}, [c]) for n, c in self.choices.items()]


def _record(store: FakeStore, case: str, *, result: str = "loss", points=(4,),
            sha: str | None = None, judge: str | None = None, at: str = "t") -> None:
    cfg = fixer_eval.config()
    store.append_run({"phase": "replay:instance", "started": at, "finished": at,
                      "trigger": "cli", "stats": {
                          "case": case, "issue": int(case.split("-")[1]) if "-" in case else int(case),
                          "ending": "fixed", "our_fix": OURS, "flags": ["foreign-text"],
                          "fixer": {"prompt_sha": sha or cfg["prompt_sha"], "models": cfg["model"]},
                          "craft": {"result": result, "points": list(points), "reasons": ["r1", "r2"],
                                    "judge_sha": judge or craft_judge.JUDGE_SHA}}})


def _jobs(*cases: str):
    def jobs(only):
        return {c: types.SimpleNamespace(case=c, instance=types.SimpleNamespace(
            report_title=f"title {c}", landed_diff=REF)) for c in cases if c in only}
    return jobs


def test_losses_are_this_configuration_s_under_today_s_judge():
    store = FakeStore()
    _record(store, "1")
    _record(store, "2", sha="other")
    _record(store, "3", judge="old")
    _record(store, "4", result="win")
    _record(store, "5", result="tie", points=())
    got = fixer_lessons.losses(store, FakeIssues({}), jobs=_jobs("1", "2", "3", "4", "5"))  # type: ignore[arg-type]
    assert [loss.case for loss in got] == ["1"]


def test_losses_sharing_the_commonest_point_come_first():
    store = FakeStore()
    _record(store, "1", points=(2,), at="t3")
    _record(store, "2", points=(4,), at="t1")
    _record(store, "3", result="tie", points=(4, 7), at="t2")
    got = fixer_lessons.losses(store, FakeIssues({}), jobs=_jobs("1", "2", "3"))  # type: ignore[arg-type]
    assert [loss.case for loss in got] == ["3", "2", "1"]


def test_a_loss_carries_the_reference_and_the_operator_s_words():
    store = FakeStore()
    _record(store, "rival-9-90")
    store.verdicts[9] = [types.SimpleNamespace(label={"note": "SQL in strings again"}),
                         types.SimpleNamespace(label={"note": ""})]
    issues = FakeIssues({9: {"pr": 90, "reason": "the outside PR uses typed values"}})
    [loss] = fixer_lessons.losses(store, issues, jobs=_jobs("rival-9-90"))  # type: ignore[arg-type]
    assert (loss.issue, loss.title, loss.result, loss.points) == (9, "title rival-9-90", "loss", [4])
    assert loss.reference == REF and loss.ours == OURS and loss.flags == ["foreign-text"]
    assert loss.notes == ["the outside PR uses typed values", "SQL in strings again"]


def test_a_case_no_longer_planned_is_skipped():
    store = FakeStore()
    _record(store, "1")
    _record(store, "2")
    got = fixer_lessons.losses(store, FakeIssues({}), jobs=_jobs("2"))  # type: ignore[arg-type]
    assert [loss.case for loss in got] == ["2"]
