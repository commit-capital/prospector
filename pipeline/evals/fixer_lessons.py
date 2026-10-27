"""The fixer's improvement loop (docs/superpowers/specs/2026-10-24-fixer-ratchet-design.md):
read where the code's fixer lost on craft against the human fix, ask one
locked-down agent for one general rewrite of the house style or a fixer prompt
that would have prevented the commonest loss, check it on the host, and write
it as a local diff. Nothing is committed or recorded; an applied diff fails the
fixer pin test, and the fixer gate decides whether it stays.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from issue_triage.issue_store import IssueStore
from pipeline.evals import craft_judge, eval_set, fixer_eval
from pipeline.store import Store

Jobs = Callable[[set[str]], dict[str, eval_set._Job]]


@dataclass(frozen=True)
class Loss:
    case: str
    issue: int
    title: str
    ours: str
    reference: str
    result: str
    reasons: list[str]
    points: list[int]
    flags: list[str]
    notes: list[str]


def _planned(store: Store) -> Jobs:
    def jobs(only: set[str]) -> dict[str, eval_set._Job]:
        return {j.case: j for j in eval_set.plan_jobs(
            store, eval_set.load_manifest(store), refresh=False, issues=None, only=only,
            cases=True)}
    return jobs


def _notes(store: Store, choices: dict[int, dict], issue: int) -> list[str]:
    """The operator's words on `issue`: why they chose another PR, and their
    notes on its labeled reviews."""
    said = [str((choices.get(issue) or {}).get("reason") or "")]
    said += [str((v.label or {}).get("note") or "")
             for v in store.review_verdicts(subject_kind="issue", subject=issue, labeled=True)]
    return [s.strip() for s in said if s.strip()]


def losses(store: Store, issues: IssueStore, *, jobs: Jobs | None = None,
           limit: int = 6) -> list[Loss]:
    """The code's fixer's latest craft loss per case under today's judge (a
    tie that fell short on a point counts), those sharing the commonest point
    first, then newest; a case the set no longer plans is skipped."""
    cfg = fixer_eval.config()
    latest: dict[str, dict] = {}
    for rec in store.runs(limit=eval_set._LEDGER_SCAN, phase="replay:instance"):
        stats = rec.raw.get("stats") or {}
        fixer, craft = stats.get("fixer") or {}, stats.get("craft") or {}
        if ((fixer.get("prompt_sha"), fixer.get("models")) == (cfg["prompt_sha"], cfg["model"])
                and craft.get("judge_sha") == craft_judge.JUDGE_SHA
                and (craft.get("result") == "loss"
                     or (craft.get("result") == "tie" and craft.get("points")))):
            latest[eval_set._case_of(stats)] = {**stats, "at": rec.raw.get("finished") or ""}
    planned = (jobs or _planned(store))(set(latest))
    found = [stats for case, stats in latest.items() if case in planned]
    common = Counter(p for stats in found for p in stats["craft"].get("points") or [])
    found.sort(key=lambda s: (max((common[p] for p in s["craft"].get("points") or []),
                                  default=0), s["at"]), reverse=True)
    choices = {row["issue"]: choice for row, (choice,) in issues.issue_rows([("fix_choice",)])
               if isinstance(choice, dict)}
    out = []
    for stats in found[:limit]:
        case, craft = eval_set._case_of(stats), stats["craft"]
        inst = planned[case].instance
        out.append(Loss(case=case, issue=int(stats["issue"]), title=inst.report_title,
                        ours=craft_judge.source_only(str(stats.get("our_fix") or "")),
                        reference=craft_judge.source_only(inst.landed_diff),
                        result=craft["result"], reasons=list(craft.get("reasons") or []),
                        points=list(craft.get("points") or []),
                        flags=list(stats.get("flags") or []),
                        notes=_notes(store, choices, int(stats["issue"]))))
    return out
