"""Drive a locked-down headless agent over a paused merge worktree to resolve
its conflicted paths, returning the per-file rationale it records.

The worktree is a `resubmit prepare --merge` clone paused on conflicts. The
agent edits only the conflicted files (headless_agent scopes its reads, edits
and git to the worktree, and resubmit's `continue` refuses stray edits
fail-closed). The agent stages nothing and commits nothing — git writes belong
to the resubmit tool.
"""
from __future__ import annotations

import os
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
   `__GIT__ diff` shows the combined view, `__GIT__ log --merge` the commits
   on both sides that touch the conflicted files, and `__GIT__ show :2:<file>`
   / `:3:<file>` each side's whole version. It is the only git available, it
   only reads, and its paths are relative to the worktree root.
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
        "__GIT__": headless_agent.GIT_READ,
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
    worktree = os.path.realpath(worktree)
    text = headless_agent.run_agent(
        _prompt(worktree, conflict_paths, pr, title, body, base_branch),
        allow_gh=False, cwd=worktree, edit_root=worktree, read_root=worktree,
        git_root=worktree, env_allow=(),
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
