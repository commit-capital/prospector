"""Repository/deployment metadata for the app frontend.

The frontend builds every upstream PR/issue link, the tab title, and
repo-naming copy from this one object instead of embedding a deployment's
identity in the bundle. Served once at bootstrap (`/api/meta`) and cached
client-side; values come from pipeline.settings and the active repository
profile, so the backend is the single source of truth for which repository
the app is pointed at.
"""
from __future__ import annotations

from typing import TypedDict

from pipeline import profile, settings


class TestPathRules(TypedDict):
    dir_pattern: str
    file_pattern: str


class RepoMeta(TypedDict):
    repo: str
    owner: str
    name: str
    url: str
    default_branch: str
    display_name: str
    feedback_repo: str | None
    test_paths: TestPathRules


def meta() -> RepoMeta:
    """The configured repository's identity, web URL, default branch, display
    name, the repo app feedback files into (None disables feedback), and
    the active profile's test-path rules the frontend compiles its matchers
    from."""
    tp = profile.active().test_paths
    return {
        "repo": settings.repo(),
        "owner": settings.repo_owner(),
        "name": settings.repo_name(),
        "url": settings.repo_url(),
        "default_branch": settings.default_branch(),
        "display_name": settings.display_name(),
        "feedback_repo": settings.feedback_repo() or None,
        "test_paths": {"dir_pattern": tp.dir_pattern, "file_pattern": tp.file_pattern},
    }
