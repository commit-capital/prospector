from prospector_app.backend import instance


def setup_function():
    instance._cache = None


def teardown_function():
    instance._cache = None


def test_instance_reports_branch_and_worktree():
    info = instance.instance()
    # Running inside the repo's own git checkout, both resolve.
    assert info["branch"]
    assert info["worktree"]


def test_instance_is_cached():
    first = instance.instance()
    assert instance.instance() is first


def test_instance_keys():
    assert set(instance.instance()) == {"branch", "worktree"}


def test_git_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(instance.subprocess, "run", boom)
    assert instance._git("rev-parse", "HEAD") is None
