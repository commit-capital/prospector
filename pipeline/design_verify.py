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

_CHECKS = {
    "reuse": """\
The finding says the diff adds something the checkout already has, at
__EVIDENCE__. Check, in the checkout:
  - the cited helper exists there and is exported or otherwise reachable from
    __PATH__ without a new dependency between modules;
  - it behaves identically on every input the pull request's code handles:
    which environment variables it reads and in what precedence, how it
    resolves relative paths (against what directory), its defaults and
    fallbacks, its errors, and its return shape;
  - the pull request's own tests (in the diff) would still pass with the swap.
Any input where the two differ refutes the finding as a swap.""",
    "layering": """\
The finding says the change sits outside the layer that owns the behavior.
Grep for every caller of the code it names, count them, and say whether moving
it where the finding asks changes behavior for any of them. A move that changes
other callers' behavior or widens the change past what the pull request does is
a scope change, not a layering defect: refute it.""",
    "test": """\
The finding is about the pull request's tests. Read the test code in the diff
and the implementation it exercises, and confirm the claim against them: would
the test really still pass with the implementation broken, or does the case
really go untested? A test that does fail when the behavior breaks refutes it.""",
    "convention": """\
The finding says the code departs from a convention. Find the rule it relies on
in the checkout's contributor docs or `rules/*.mdc`, and in the surrounding
code. A rule that does not say this, or code in the checkout that already does
the same thing the rule supposedly forbids (a guard test that allows it, a
neighbouring module written the same way), refutes it. A finding that names a
defect in what the code does rather than a written rule is judged on the code:
show the input where it misbehaves, or refute it.""",
}

PROMPT = """\
Another reviewer read pull request #__PR__ ("__TITLE__") and reported the
design finding below. You did not write it. Your job is to REFUTE it: find the
reason it is wrong.

The repository's default branch is checked out at __CLONE__ — code only its
maintainers changed. The pull request's diff against it is the file __DIFF__.
Read both; grep the checkout as much as you need.

The finding (__CATEGORY__, __SEVERITY__), on __PATH____LINE__:
  __FINDING_TITLE__
  __BODY__

__CHECKS__

A finding whose point is who may change this code, a trust or permission
boundary, secrets, credentials, CI and release configuration, or any other
security concern is not a design finding, and a maintainer may make the same
change: refute it.

A wrong finding is costly: it holds a contributor's merge and becomes a bot's
change on their branch. Say `confirmed` only if you checked every point above
against the code, the finding held on each, and it is a defect a maintainer
would hold the merge for. One about comments, docs or naming never is, whatever
the contributor docs say: a true finding below that bar is `unsure`. Say `refuted` when the code
shows it is wrong, naming the input, caller or line that shows it. Say `unsure`
when you could not settle it or it is true but minor.

Your final message is exactly one JSON object, nothing else:
  {"verdict": "confirmed" | "refuted" | "unsure",
   "reason": "<two or three sentences: what you checked and what decided it>"}
"""


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
