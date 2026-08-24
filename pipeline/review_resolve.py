"""Judge an agent's merge-conflict resolution before it can be pushed
unattended, by asking a second agent to refute it.

Same contract as review_fix: the reviewer runs in its own process with
read-only tools, no network, `cwd` at the merge worktree. Only a well-formed,
explicit `safe` returns safe; a refusal, malformed answer, timeout, or crashed
process returns `unsafe`, so autopush is unreachable by breaking the reviewer.
Each of a resolve's two reviewers runs under its own lens, looking for a
different family of failures.
"""
from __future__ import annotations

from collections.abc import Callable

from pipeline import headless_agent

AGENT_TIMEOUT_SECONDS = 900

# How much of each diff the reviewer is shown; the tail is kept whole because a
# smuggled edit would sit at the end.
DIFF_HEAD_CHARS = 40_000
DIFF_TAIL_CHARS = 15_000

LENSES: dict[str, str] = {
    "behavior": """\
Judge whether the resolved code preserves BOTH sides' behavior:
  - a hunk from either side silently dropped or half-kept
  - a guard, validation, or error path one side added that the resolution loses
  - callers, subclasses, or tests outside the conflicted lines that the merged
    result breaks
  - resolved code that compiles but combines the sides into something neither
    intended""",
    "history": """\
From the per-side commit history and the pull request context below, work out
what each side's change was FOR — a bug fix, a feature, a revert — then judge
whether the resolution keeps the earlier changes' purpose intact while landing
the new one:
  - reason about how each earlier change would be exercised (its repro steps,
    its tests) and whether that still works in the resolved code
  - a fix from one side quietly undone counts as a regression even when the
    resolved file looks tidy
  - if a side's purpose cannot be determined from the evidence and the code,
    say so as a concern rather than assuming it survived""",
}

PROMPT = """\
A bot resolved the merge conflicts below on pull request #__PR__ ("__TITLE__"),
an outside contribution to this project. A merge of the base branch into the
PR's branch had paused on conflicts; the bot edited the conflicted files and
the result has not been pushed. You decide whether it may be.

__LENS__

The worktree at __WORKTREE__ holds the merged result (HEAD is the merge
commit; HEAD^1 is the PR's branch, HEAD^2 the base). Read whatever you need
there — `git log`, `git show`, and both parents are available.

What the pull request is about, from this project's triage records:
__STORE_CONTEXT__

Commits that produced each side of the conflicted files:
__HISTORY__

The conflicted regions as git presented them:
```
__MERGE_DIFF__
```

The bot's stated rationale per file:
__RESOLUTIONS__

The bot's final change against the PR's branch:
```
__PATCH__
```

Default to "unsafe". Say "safe" only if you looked and found nothing — not
because nothing jumped out. A wrong rejection parks this for a human, costing
one click. A wrong approval pushes a bot's guess onto a stranger's branch
under this project's name, unattended.

Your final message must be exactly one JSON object, nothing else:
  {"verdict": "safe" | "unsafe",
   "reason": "<one or two sentences: what you checked, or what is wrong>",
   "concerns": ["<each specific problem, if any>"]}
"""


def _clip(text: str) -> str:
    if len(text) <= DIFF_HEAD_CHARS + DIFF_TAIL_CHARS:
        return text
    cut = len(text) - DIFF_HEAD_CHARS - DIFF_TAIL_CHARS
    return (text[:DIFF_HEAD_CHARS]
            + f"\n\n[... {cut} characters omitted ...]\n\n"
            + text[-DIFF_TAIL_CHARS:])


def _resolutions_block(resolutions: list[dict]) -> str:
    if not resolutions:
        return "  (none recorded)"
    return "\n".join(f"  - {str(r.get('path') or '?')}: "
                     f"{str(r.get('rationale') or '').strip()}"
                     for r in resolutions)


def _prompt(worktree: str, *, pr: int, title: str, merge_diff: str, patch: str,
            resolutions: list[dict], history: str, store_context: str,
            lens: str) -> str:
    return headless_agent.fill(PROMPT, {
        "__PR__": pr,
        "__TITLE__": title.strip(),
        "__LENS__": LENSES[lens],
        "__WORKTREE__": worktree,
        "__STORE_CONTEXT__": store_context.strip() or "  (nothing recorded)",
        "__HISTORY__": history.strip() or "  (no history available)",
        "__MERGE_DIFF__": _clip(merge_diff),
        "__RESOLUTIONS__": _resolutions_block(resolutions),
        "__PATCH__": _clip(patch),
    })


def _unsafe(reason: str, failed: bool = False) -> dict:
    out: dict = {"verdict": "unsafe", "reason": reason, "concerns": []}
    if failed:
        out["failed"] = True
    return out


def review(worktree: str, *, pr: int, title: str, merge_diff: str, patch: str,
           resolutions: list[dict], history: str, store_context: str,
           lens: str, on_event: Callable[[tuple], None] | None = None) -> dict:
    """Judge the resolution through `lens`, returning
    {"verdict": "safe"|"unsafe", "reason": str, "concerns": list[str]}.

    Only a well-formed, explicit `safe` returns safe. A reviewer that never
    reached a verdict — it crashed, timed out, or answered without one — also
    carries `failed: True`, so the caller can tell a judgment on the change
    from the machine's failure to judge it."""
    if lens not in LENSES:
        raise ValueError(f"unknown review lens: {lens!r}")
    try:
        text = headless_agent.run_agent(
            _prompt(worktree, pr=pr, title=title, merge_diff=merge_diff,
                    patch=patch, resolutions=resolutions, history=history,
                    store_context=store_context, lens=lens),
            allow_gh=False, cwd=worktree, edit_root=None,
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
