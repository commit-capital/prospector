"""The design review's second pass: one agent per blocking or major finding,
from a fresh context, asked to refute it.

A design finding is one agent's judgment, and a confident wrong one ("swap this
for the existing helper" when the helper resolves paths differently) would fail
the bar and become an autofix goal. Each verifier gets the reviewer's sandbox —
the pinned base clone and the head's diff, read-only, no GitHub, no environment
beyond the CLI's own — and the one finding, and answers `confirmed`, `refuted`
or `unsure`. A verifier that gives no usable answer leaves its finding
`unverified`.

`verify` raises only what `headless_agent` and `capacity` raise for the machine
(an agent outage, a spent usage limit, an overload, a closed capacity gate), so
the review is retried whole rather than stored half-judged.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pipeline import capacity, headless_agent, reviewers

AGENT_TIMEOUT_SECONDS = 900
REASON_CHARS = 1000

CONFIRMED, REFUTED, UNSURE, UNVERIFIED = "confirmed", "refuted", "unsure", "unverified"
VERDICTS = (CONFIRMED, REFUTED, UNSURE, UNVERIFIED)

_CHECKS = {k: headless_agent.prompt(f"design/verify-{k}.md")
           for k in ("reuse", "layering", "test", "convention")}

PROMPT = headless_agent.prompt("design/verify.md")


def _prompt(finding: dict, *, clone: Path, diff: Path, pr: int, title: str) -> str:
    category = str(finding.get("category") or "")
    line = finding.get("line")
    return headless_agent.fill(PROMPT, {
        "__PR__": pr, "__TITLE__": title, "__CLONE__": clone, "__DIFF__": diff,
        "__CATEGORY__": category, "__SEVERITY__": finding.get("severity"),
        "__PATH__": finding.get("path"), "__LINE__": f":{line}" if isinstance(line, int) else "",
        "__FINDING_TITLE__": finding.get("title"), "__BODY__": finding.get("body") or "",
        "__CHECKS__": headless_agent.fill(_CHECKS.get(category, _CHECKS["convention"]), {
            "__EVIDENCE__": finding.get("evidence_path") or "(no path given)",
            "__PATH__": finding.get("path")})})


def verify(finding: dict, *, clone: Path, diff: Path, pr: int, title: str) -> dict:
    """`finding`'s verification: {"verdict": confirmed | refuted | unsure |
    unverified, "reason": str}."""
    prompt = _prompt(finding, clone=clone, diff=diff, pr=pr, title=title)

    def run() -> str:
        return headless_agent.run_agent(
            prompt, allow_gh=False, cwd=str(clone), edit_root=None,
            read_root=[str(clone), str(diff)], env_allow=(), timeout=AGENT_TIMEOUT_SECONDS)

    try:
        answer, _ = headless_agent.json_reply(run)
    except (headless_agent.AgentUnavailable, headless_agent.AgentTransient,
            capacity.CapacityPaused):
        raise
    except (RuntimeError, ValueError) as e:
        return {"verdict": UNVERIFIED, "reason": f"the verifier did not finish: {e}"[:REASON_CHARS]}
    verdict = answer.get("verdict")
    if verdict not in (CONFIRMED, REFUTED, UNSURE):
        return {"verdict": UNVERIFIED, "reason": f"the verifier gave no verdict: {verdict!r}"}
    return {"verdict": verdict, "reason": str(answer.get("reason") or "")[:REASON_CHARS]}


def apply(findings: list[dict], verify_one: Callable[[dict], dict]) -> list[dict]:
    """`findings` with every blocking or major one verified by `verify_one` and
    carrying its `verification`: an `unsure` one is downgraded to minor, its
    original severity kept as `claimed_severity`."""
    out: list[dict] = []
    for f in findings:
        if f.get("severity") not in reviewers.DESIGN_BLOCKING:
            out.append(f)
            continue
        v = verify_one(f)
        f = {**f, "verification": v}
        if v["verdict"] == UNSURE:
            f = {**f, "severity": "minor", "claimed_severity": f["severity"]}
        out.append(f)
    return out
