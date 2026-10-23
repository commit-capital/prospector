"""Judge an agent-authored patch before it can be pushed to a contributor's
branch, by asking a second agent to refute it.

The reviewer runs in its own process with no memory of writing the code and no
ability to change it: read-only tools held to the worktree, no network, the
CLI's bare environment, `cwd` at the worktree so it can read the code the patch
lands in. Its prompt asks for a reason to reject
rather than an opinion, because an author grading its own work is the one
verdict worth nothing.

The change is read twice, by a reviewer per lens (`LENSES`): `scope-safety`
looks for harm beyond the goal, and `design` asks whether a maintainer would
merge it as written (`authoring.DESIGN_QUESTION`, the question the issue-fix
lane asks too). Either one's rejection rejects the change.

`review` never raises. Every way of not getting an explicit `safe` from both —
a refusal, a malformed answer, a timeout, a crashed process — returns `unsafe`,
so the push side is unreachable by breaking a reviewer.
"""
from __future__ import annotations

import os
from collections.abc import Callable

from pipeline import authoring, headless_agent

# Reading a patch and its surrounding code, without writing any.
AGENT_TIMEOUT_SECONDS = 900

# How much patch the reviewer is shown. A change this size is already past what
# an operator would approve by eye, and the tail is where a smuggled edit would
# sit — so the cap truncates the middle rather than the end.
PATCH_HEAD_CHARS = 60_000
PATCH_TAIL_CHARS = 20_000

PROMPT = """\
A bot wrote the change below onto the head branch of pull request #__PR__, an
outside contribution to this project. It has not been pushed. You decide
whether it may be.

The bot was asked to:
__GOAL__

__FINDINGS____SUMMARY__The worktree at __WORKTREE__ holds the code this change applies to. Read
whatever you need there.

__QUESTION__

Judge the change as written, not the goal it was aimed at: a change that fixes
the right problem the wrong way is unsafe.

Default to "unsafe". Say "safe" only if you looked and found nothing — not
because nothing jumped out. A wrong rejection costs someone one click. A wrong
approval puts a bot's guess on a stranger's branch under this project's name.

The change:
```
__PATCH__
```

Your final message must be exactly one JSON object, nothing else:
  {"verdict": "safe" | "unsafe",
   "reason": "<one or two sentences: what you checked, or what is wrong>",
   "concerns": ["<each specific problem, if any>"]}
"""

# The one question each reviewer asks of the change, keyed by LENSES.
_QUESTIONS = {
    "scope-safety": """\
Your job is to REFUTE this change — to find the reason it must not be pushed.
Look for:
  - behavior that changes beyond what the goal asked for
  - edits to files, lines, or formatting the goal did not require
  - callers, subclasses, or tests outside the diff that this breaks
  - a test weakened, skipped, deleted, or made to pass without fixing anything
  - error handling, validation, or a boundary check quietly removed
  - a guess at intent that the pull request's author would dispute
  - anything that reads as plausible but that you cannot verify against the code""",
    "design": ("Your job is to REFUTE this change on its design alone — to find the "
               "reason a maintainer would not merge it. " + authoring.DESIGN_QUESTION),
}

LENSES = tuple(_QUESTIONS)


# The review provider's summary is prose the reviewer reads for context only.
SUMMARY_CHARS = 4000


def _clip(patch: str) -> str:
    """The patch, truncating the middle when it is enormous — a smuggled edit
    would sit at the end, so the end is kept."""
    if len(patch) <= PATCH_HEAD_CHARS + PATCH_TAIL_CHARS:
        return patch
    cut = len(patch) - PATCH_HEAD_CHARS - PATCH_TAIL_CHARS
    return (patch[:PATCH_HEAD_CHARS]
            + f"\n\n[... {cut} characters of this patch omitted ...]\n\n"
            + patch[-PATCH_TAIL_CHARS:])


def _findings_block(findings: list[dict] | None) -> str:
    if not findings:
        return ""
    lines = ["The review findings it was aimed at:"]
    lines += [f"  - {str(f.get('headline') or '').strip()}" for f in findings]
    return "\n".join(lines) + "\n\n"


def _summary_block(review_summary: str) -> str:
    if not review_summary.strip():
        return ""
    return ("The review summary it was aimed at:\n"
            + review_summary.strip()[:SUMMARY_CHARS] + "\n\n")


def _prompt(worktree: str, patch: str, pr: int, goal: str,
            findings: list[dict] | None, review_summary: str, lens: str) -> str:
    return headless_agent.fill(PROMPT, {
        "__PR__": pr,
        "__GOAL__": goal.strip(),
        "__FINDINGS__": _findings_block(findings),
        "__SUMMARY__": _summary_block(review_summary),
        "__WORKTREE__": worktree,
        "__QUESTION__": _QUESTIONS[lens],
        "__PATCH__": _clip(patch),
    })


def _unsafe(reason: str, failed: bool = False) -> dict:
    out: dict = {"verdict": "unsafe", "reason": reason, "concerns": []}
    if failed:
        out["failed"] = True
    return out


def review(worktree: str, patch: str, *, pr: int, goal: str,
           findings: list[dict] | None = None, review_summary: str = "",
           on_step: Callable[[str], None] = lambda step: None,
           on_event: Callable[[tuple], None] | None = None) -> dict:
    """Judge `patch` against `goal` under every lens in LENSES, one reviewer
    after another (`on_step` names each as it starts), returning
    {"verdict": "safe"|"unsafe", "reason": str, "concerns": list[str]}.

    Only a well-formed, explicit `safe` from every lens returns safe. A lens
    that rejects the change rejects it, its reason and concerns named by lens.
    A lens that never reached a verdict — it crashed, timed out, or answered
    without one — makes the answer unsafe with `failed: True` when no other
    lens judged it unsafe (`verdict_of`), so the caller can tell a judgment on
    the change from the machine's failure to judge it."""
    worktree = os.path.realpath(worktree)
    answers: dict[str, dict] = {}
    for lens in LENSES:
        on_step(f"reviewing the authored change: {lens}")
        answers[lens] = _judge(
            _prompt(worktree, patch, pr, goal, findings, review_summary, lens),
            worktree, on_event)
    verdict = verdict_of(list(answers.values()))
    # The lenses the answer speaks for: every one when all passed, else the
    # rejections, else the failures.
    named = {lens: a for lens, a in answers.items()
             if verdict == "safe"
             or (a.get("failed") if verdict == "failed" else a["verdict"] == "unsafe"
                 and not a.get("failed"))}
    out: dict = {
        "verdict": "safe" if verdict == "safe" else "unsafe",
        "reason": "; ".join(f"{lens}: {a['reason']}" for lens, a in named.items()
                            if a["reason"]),
        "concerns": [f"{lens}: {c}" for lens, a in named.items() for c in a["concerns"]],
    }
    if verdict == "failed":
        out["failed"] = True
    return out


def verdict_of(reviews: list[dict]) -> str:
    """A run's fix-review verdict over its per-lens `reviews`: `unsafe` when a
    review judged the change unsafe, else `failed` when any review did not
    finish."""
    judged = [r for r in reviews if not r.get("failed")]
    return ("unsafe" if any(r["verdict"] == "unsafe" for r in judged)
            else "failed" if len(judged) < len(reviews) else "safe")


def _judge(prompt: str, worktree: str,
           on_event: Callable[[tuple], None] | None) -> dict:
    """One reviewer's answer to `prompt`, as `review` reads it."""
    try:
        text = headless_agent.run_agent(
            prompt, allow_gh=False, cwd=worktree, edit_root=None, read_root=worktree,
            env_allow=(),
            timeout=AGENT_TIMEOUT_SECONDS, on_event=on_event)
    except RuntimeError as e:
        return _unsafe(f"the reviewing agent did not finish: {e}", failed=True)
    try:
        verdict = headless_agent.extract_json(text)
    except ValueError:
        return _unsafe(f"the reviewing agent gave no usable verdict: {text[-300:]}",
                       failed=True)
    raw = verdict.get("concerns")
    concerns = [str(c) for c in raw] if isinstance(raw, list) else []
    if verdict.get("verdict") != "safe":
        return {"verdict": "unsafe",
                "reason": str(verdict.get("reason")
                              or "the reviewing agent did not return a verdict"),
                "concerns": concerns}
    return {"verdict": "safe", "reason": str(verdict.get("reason") or ""),
            "concerns": concerns}
