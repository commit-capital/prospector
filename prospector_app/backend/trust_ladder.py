"""The trust ladder: how much unattended reach each autonomous action type has
earned, derived on read from captured human decisions and autofix endings.

Each action type sits on a rung — ``shadow`` (the agent only logs its pick),
``one-click`` (a person approves each action), ``auto+undo`` (the agent acts,
a person reviews afterward), ``auto`` (the agent acts, a person spot-checks) —
computed fresh on every read from two live rates, so a spike in reversals
demotes a type with no human action:

- **agreement** — how often a person accepts the agent's pick. Upstream
  decision types (``merge``, ``close-dup``, ``close-fixed``) compare each live
  terminal decision against the suggested disposition its capture recorded; a
  capture without a recorded suggestion carries no evidence. Autofix types
  (``update``, ``rebase``, ``resolve``, ``fix``, ``describe``) read the runs
  ledger's ``fix:single`` lane: a ``pushed`` ending is an acceptance and a
  ``cancelled`` ending a rejection, whoever cancelled it.
- **reversal** — how often the automation's work is undone or rejected. A live
  close later REOPENed counts against its close type; a ``refused`` autofix
  ending (a gate or reviewer rejecting the prepared change) counts against its
  action. A merge has no undo signal, so its reversal carries no evidence.

The rung is a disclosure: the Policy page renders it, and the env-configured
autopush and hunt switches remain the enforcement.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline import storekit


RUNGS: tuple[str, ...] = ("shadow", "one-click", "auto+undo", "auto")

#: Every action type on the ladder: the autofix actions the worker runs, then
#: the upstream dispositions a person executes today.
ACTION_TYPES: tuple[str, ...] = (
    "update", "rebase", "resolve", "fix", "describe",
    "close-dup", "close-fixed", "merge",
)

#: The most recent judged events per type that the rates read.
WINDOW = 200


@dataclass(frozen=True)
class Bar:
    """What a type's window must show to sit at (or above) one rung."""
    min_decisions: int
    min_agreement: float
    max_reversal: float


#: Each rung above shadow, with the bar that earns it. Fail-closed: a type
#: whose agreement window is thinner than ``min_decisions`` sits below the
#: rung whatever its rates say.
PROMOTION: dict[str, Bar] = {
    "one-click": Bar(min_decisions=10, min_agreement=0.70, max_reversal=0.25),
    "auto+undo": Bar(min_decisions=30, min_agreement=0.90, max_reversal=0.10),
    "auto": Bar(min_decisions=100, min_agreement=0.98, max_reversal=0.02),
}


@dataclass(frozen=True)
class Rate:
    """``hits`` of ``n`` judged events; ``rate`` reads 0.0 with no evidence."""
    hits: int
    n: int

    @property
    def rate(self) -> float:
        return self.hits / self.n if self.n else 0.0

    def as_dict(self) -> dict:
        return {"hits": self.hits, "n": self.n, "rate": round(self.rate, 4)}


NO_EVIDENCE = Rate(0, 0)

#: The capture verb a person's confirmation of each upstream pick lands as.
_DECISION_FOR: dict[str, str] = {
    "merge": "MERGE", "close-dup": "CLOSE_DUP", "close-fixed": "CLOSE_FIXED",
}

#: Capture verbs that are a terminal ruling on a PR. Comments and reviews
#: other than request-changes leave the pick undecided, so they carry no
#: agreement evidence.
_VERDICTS = frozenset({
    "MERGE", "CLOSE", "CLOSE_DUP", "CLOSE_FIXED", "CLOSE_STALE",
    "REVIEW:request-changes",
})

_CLOSE_TYPE: dict[str, str] = {"CLOSE_DUP": "close-dup", "CLOSE_FIXED": "close-fixed"}

_LEDGER_TYPES = frozenset({"update", "rebase", "resolve", "fix", "describe"})


def _windowed(judged: list[bool]) -> Rate:
    tail = judged[-WINDOW:]
    return Rate(sum(tail), len(tail))


def upstream_rates(rows: list[dict]) -> dict[str, tuple[Rate, Rate]]:
    """(agreement, reversal) for the upstream types from captured decisions,
    oldest first. Agreement buckets by the suggested disposition each capture
    recorded — of the agent's X picks a person then ruled on, how many they
    confirmed. Reversal pairs each live close with any later live REOPEN of
    the same PR. Dry-run captures are previews and carry no evidence."""
    live = [r for r in rows if not r.get("dry_run")]
    reopens: dict[int, list[str]] = {}
    for r in live:
        if r.get("decision") == "REOPEN" and isinstance(r.get("pr"), int):
            reopens.setdefault(r["pr"], []).append(str(r.get("at") or ""))
    agree: dict[str, list[bool]] = {t: [] for t in _DECISION_FOR}
    reverse: dict[str, list[bool]] = {t: [] for t in _DECISION_FOR}
    for r in live:
        decision = r.get("decision")
        suggested = (r.get("features") or {}).get("disposition")
        if suggested in _DECISION_FOR and decision in _VERDICTS:
            agree[suggested].append(decision == _DECISION_FOR[suggested])
        close_type = _CLOSE_TYPE.get(decision or "")
        if close_type and isinstance(r.get("pr"), int):
            at = str(r.get("at") or "")
            undone = any(re_at > at for re_at in reopens.get(r["pr"], []))
            reverse[close_type].append(undone)
    return {t: (_windowed(agree[t]), _windowed(reverse[t]) if t in _CLOSE_TYPE.values()
                else NO_EVIDENCE)
            for t in _DECISION_FOR}


def ledger_rates(runs: list[storekit.RunRecord]) -> dict[str, tuple[Rate, Rate]]:
    """(agreement, reversal) for the autofix types from the runs ledger's
    ``fix:single`` endings, in insertion order. ``pushed`` accepts and
    ``cancelled`` rejects the prepared change; ``refused`` is the gate or
    reviewer rejecting it, which is the reversal signal. Intermediate and
    machine endings (``awaiting-review``, ``approved``, ``failed``) judge
    nothing."""
    accepted: dict[str, list[bool]] = {t: [] for t in _LEDGER_TYPES}
    rejected: dict[str, list[bool]] = {t: [] for t in _LEDGER_TYPES}
    for rec in runs:
        if not isinstance(rec, storekit.PhaseRun) or rec.phase != "fix:single":
            continue
        stats = rec.raw.get("stats") or {}
        action, status = stats.get("action"), stats.get("status")
        if action not in _LEDGER_TYPES:
            continue
        if status in ("pushed", "cancelled"):
            accepted[action].append(status == "pushed")
            rejected[action].append(False)
        elif status == "refused":
            rejected[action].append(True)
    return {t: (_windowed(accepted[t]), _windowed(rejected[t])) for t in _LEDGER_TYPES}


def rung(agreement: Rate, reversal: Rate) -> str:
    """The highest rung the rates earn, climbing from shadow and stopping at
    the first bar missed — so a reversal spike or a thinned window demotes on
    the read that sees it."""
    earned = "shadow"
    for name in RUNGS[1:]:
        bar = PROMOTION[name]
        if (agreement.n >= bar.min_decisions
                and agreement.rate >= bar.min_agreement
                and reversal.rate <= bar.max_reversal):
            earned = name
        else:
            break
    return earned


def ladder() -> dict:
    """Every action type's rung and live rates, with the bars they are held
    to — the Policy page's data. Computed fresh from the store on each call;
    nothing is stored."""
    from prospector_app.backend import data
    from prospector_app.backend import training

    rates = ledger_rates(data.runs())
    rates.update(upstream_rates(training.decisions()))
    types = []
    for t in ACTION_TYPES:
        agreement, reversal = rates.get(t, (NO_EVIDENCE, NO_EVIDENCE))
        types.append({
            "id": t,
            "rung": rung(agreement, reversal),
            "agreement": agreement.as_dict(),
            "reversal": reversal.as_dict(),
        })
    return {
        "rungs": list(RUNGS),
        "window": WINDOW,
        "bars": {name: {"min_decisions": b.min_decisions,
                        "min_agreement": b.min_agreement,
                        "max_reversal": b.max_reversal}
                 for name, b in PROMOTION.items()},
        "types": types,
    }
