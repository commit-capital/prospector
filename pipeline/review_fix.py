"""Judge an agent-authored patch before it can be pushed to a contributor's
branch, by asking a second agent to refute it.

The reviewer runs in its own process with no memory of writing the code and no
ability to change it: read-only tools, no network, `cwd` at the worktree so it
can read the code the patch lands in. Its prompt asks for a reason to reject
rather than an opinion, because an author grading its own work is the one
verdict worth nothing.

`review` never raises. Every way of not getting an explicit `safe` — a refusal,
a malformed answer, a timeout, a crashed process — returns `unsafe`, so the
push side is unreachable by breaking the reviewer.
"""
from __future__ import annotations

from collections.abc import Callable

from pipeline import headless_agent

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

Your job is to REFUTE this change — to find the reason it must not be pushed.
Look for:
  - behavior that changes beyond what the goal asked for
  - edits to files, lines, or formatting the goal did not require
  - callers, subclasses, or tests outside the diff that this breaks
  - a test weakened, skipped, deleted, or made to pass without fixing anything
  - error handling, validation, or a boundary check quietly removed
  - a guess at intent that the pull request's author would dispute
  - anything that reads as plausible but that you cannot verify against the code

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
            findings: list[dict] | None, review_summary: str) -> str:
    return headless_agent.fill(PROMPT, {
        "__PR__": pr,
        "__GOAL__": goal.strip(),
        "__FINDINGS__": _findings_block(findings),
        "__SUMMARY__": _summary_block(review_summary),
        "__WORKTREE__": worktree,
        "__PATCH__": _clip(patch),
    })


def _unsafe(reason: str) -> dict:
    return {"verdict": "unsafe", "reason": reason, "concerns": []}


def review(worktree: str, patch: str, *, pr: int, goal: str,
           findings: list[dict] | None = None, review_summary: str = "",
           on_event: Callable[[tuple], None] | None = None) -> dict:
    """Judge `patch` against `goal`, returning
    {"verdict": "safe"|"unsafe", "reason": str, "concerns": list[str]}.

    Only a well-formed, explicit `safe` returns safe; every other outcome,
    including a failure of the reviewer itself, returns unsafe with what went
    wrong as the reason."""
    try:
        text = headless_agent.run_agent(
            _prompt(worktree, patch, pr, goal, findings, review_summary),
            allow_gh=False, cwd=worktree, edit_root=None,
            timeout=AGENT_TIMEOUT_SECONDS, on_event=on_event)
    except RuntimeError as e:
        return _unsafe(f"the reviewing agent did not finish: {e}")
    try:
        verdict = headless_agent.extract_json(text)
    except ValueError:
        return _unsafe(f"the reviewing agent gave no usable verdict: {text[-300:]}")
    if verdict.get("verdict") != "safe":
        raw = verdict.get("concerns")
        concerns = [str(c) for c in raw] if isinstance(raw, list) else []
        return {"verdict": "unsafe",
                "reason": str(verdict.get("reason")
                              or "the reviewing agent did not return a verdict"),
                "concerns": concerns}
    raw = verdict.get("concerns")
    return {"verdict": "safe", "reason": str(verdict.get("reason") or ""),
            "concerns": [str(c) for c in raw] if isinstance(raw, list) else []}
