"""The ONE CODEOWNERS policy for the configured repo.

A triaged repository's default-branch ruleset may require a code owner's
approval/merge for a set of infrastructure paths. The bot has `contents:
write` and can merge everything else, but a bot merge of a code-owner-gated
path would either be blocked by branch protection or, worse, bypass an
intended human gate. So the cockpit must detect these PRs and route them to a
human owner instead of auto-merging (#15, #26).

The gated globs and owners are repository policy in the active profile
(pipeline/profile.py `codeowners`); the generic default gates nothing. This is
the single source of truth for the path→owner map; the merge gate and the
cockpit both consume it.
"""
from __future__ import annotations

from collections.abc import Iterable

from pipeline import diffpaths, profile


def is_gated(path: str) -> bool:
    """True if a single changed path requires a code owner's merge."""
    p = diffpaths.normalize_path(path)
    if not p:
        return False
    return any(diffpaths.matches_glob(p, g)
               for g in profile.active().codeowners.gated_globs)


def gated_paths(paths: Iterable[str]) -> list[str]:
    """The subset of changed paths that are code-owner-gated."""
    return [p for p in paths if is_gated(p)]


def human_merge(paths: Iterable[str]) -> dict | None:
    """If any changed path is gated, describe the required human merge:
    {"required": True, "paths": [...gated...], "owners": [...]}. Else None."""
    gated = gated_paths(paths)
    if not gated:
        return None
    return {"required": True, "paths": gated,
            "owners": list(profile.active().codeowners.owners)}
