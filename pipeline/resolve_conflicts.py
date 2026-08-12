"""Drive a locked-down headless agent over a paused merge worktree to resolve
its conflicted paths, returning the per-file rationale it records.

The worktree is a `resubmit prepare --merge` clone paused on conflicts. The
agent edits only the conflicted files (headless_agent's edit_root scopes its
Edit/Write to the worktree, and resubmit's `continue` refuses stray edits
fail-closed). The agent stages nothing and commits nothing — git writes belong
to the resubmit tool.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict

from pipeline import headless_agent

# Generous but bounded: a resolution is a handful of file edits, not a build.
AGENT_TIMEOUT_SECONDS = 900


class Resolution(TypedDict):
    path: str
    rationale: str


PROMPT = """\
You are resolving merge conflicts in the git worktree at __WORKTREE__.
A merge of the base branch '__BASE__' into this pull request's branch is
paused on conflicts. Your job is to edit the conflicted files so both sides'
intent is preserved, then report what you did as JSON.

The pull request (PR #__PR__): __TITLE__

__BODY__

Conflicted files (resolve ALL of these, and ONLY these):
__PATHS__

For each conflicted file:
1. Read it. Conflict regions look like:
   <<<<<<< HEAD          (this PR's branch — "ours")
   ...
   =======
   ...
   >>>>>>> <sha>         (the base branch — "theirs")
   `git diff` in the worktree shows the combined view; `git log` shows both
   histories.
2. Edit the file to remove every conflict marker, keeping BOTH sides' intent
   whenever they do not genuinely contradict — for example, two independent
   additions at the same location are both kept.
3. Do not modify any other file. Do not stage, commit, or run any git command
   that writes.

If the two sides genuinely contradict — the same behavior implemented two
incompatible ways, where choosing is a product decision — do not guess.
Give up instead.

Your final message must be exactly one JSON object, nothing else:
  {"resolutions": [{"path": "<file>", "rationale": "<one or two sentences on
   how you combined the sides>"}, ...]}   — one entry per conflicted file
or
  {"give_up": "<one or two sentences on why a person must decide>"}
"""


def _prompt(worktree: str, conflict_paths: list[str], pr: int, title: str, body: str,
            base_branch: str) -> str:
    return headless_agent.fill(PROMPT, {
        "__WORKTREE__": worktree,
        "__BASE__": base_branch,
        "__PR__": pr,
        "__TITLE__": title,
        "__BODY__": (body or "(no description)").strip()[:4000],
        "__PATHS__": "\n".join(f"  {p}" for p in conflict_paths),
    })


def resolve(worktree: str, conflict_paths: list[str], *, pr: int, title: str,
            body: str, base_branch: str,
            on_event: Callable[[tuple], None] | None = None) -> dict:
    """Run the resolution agent over the paused merge at `worktree`.

    Returns the agent's verdict: {"resolutions": [Resolution, ...]} covering
    exactly the conflicted paths, or {"give_up": reason}. Raises RuntimeError
    when the agent process fails and ValueError when its output is not a
    well-formed verdict — both mean no resolution exists and the caller aborts
    the worktree."""
    text = headless_agent.run_agent(
        _prompt(worktree, conflict_paths, pr, title, body, base_branch),
        allow_gh=False, cwd=worktree, edit_root=worktree,
        timeout=AGENT_TIMEOUT_SECONDS, on_event=on_event)
    verdict = headless_agent.extract_json(text)
    if "give_up" in verdict:
        return {"give_up": str(verdict["give_up"])}
    raw = verdict.get("resolutions")
    if not isinstance(raw, list):
        raise ValueError(f"agent output has no resolutions list: {text[-500:]}")
    resolutions: list[Resolution] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError(f"malformed resolution entry: {item!r}")
        resolutions.append({"path": str(item["path"]),
                            "rationale": str(item.get("rationale") or "")})
    reported = {r["path"] for r in resolutions}
    expected = set(conflict_paths)
    if reported != expected:
        raise ValueError(
            f"agent resolutions cover {sorted(reported)} but the conflicted paths "
            f"are {sorted(expected)}")
    return {"resolutions": resolutions}
