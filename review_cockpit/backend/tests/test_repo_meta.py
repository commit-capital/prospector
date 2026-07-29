"""repo_meta.meta(): the backend-owned repository metadata object the frontend
bootstraps from (/api/meta). Everything derives from pipeline.settings, so the
suite exercises a non-Example Project repo (test-owner/test-repo) with a non-"master"
default branch (trunk) via the root conftest env."""
from __future__ import annotations

from pipeline import profile, settings
from review_cockpit.backend import repo_meta


def test_meta_derives_from_settings():
    m = repo_meta.meta()
    assert m["repo"] == settings.REPO
    assert m["repo"] == f"{m['owner']}/{m['name']}"
    assert m["url"] == f"https://github.com/{m['repo']}"
    assert m["default_branch"] == "trunk"
    assert m["display_name"] == settings.DISPLAY_NAME
    assert m["feedback_repo"] == "test-owner/test-meta-repo"


def test_no_deployment_literals():
    m = repo_meta.meta()
    joined = " ".join(str(v) for v in m.values())
    assert "example-project" not in joined.lower()
    assert "master" not in joined


def test_feedback_repo_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "FEEDBACK_REPO", "")
    assert repo_meta.meta()["feedback_repo"] is None


def test_test_paths_serve_the_active_profile_rules():
    # The fixture profile omits test_paths, so the active rules are the generic
    # defaults — the frontend compiles its test-file matchers from this field.
    generic = profile.TestPaths()
    assert repo_meta.meta()["test_paths"] == {
        "dir_pattern": generic.dir_pattern,
        "file_pattern": generic.file_pattern,
    }
