"""Evidence assembly for reviewing an agent's merge-conflict resolution."""
import subprocess

import pytest

from pipeline import model, resolve_evidence


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def merge_worktree(tmp_path):
    """A repo whose HEAD is a merge of master (theirs) into a PR branch (ours),
    with src/app.py changed on both sides."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def handle(x):\n    return x\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("from src import app\n")
    (repo / "tests" / "test_other.py").write_text("import os\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "pr")
    (repo / "src" / "app.py").write_text("def handle(x):\n    return x or 'pr'\n")
    _git(repo, "commit", "-am", "fix: handle None (#101)")
    _git(repo, "checkout", "master")
    (repo / "src" / "app.py").write_text("def handle(x):\n    return x or 'base'\n")
    _git(repo, "commit", "-am", "feat: base default (#202)")
    _git(repo, "checkout", "pr")
    r = subprocess.run(["git", "-C", str(repo), "merge", "master"],
                       capture_output=True, text=True)
    assert r.returncode != 0  # conflicts
    (repo / "src" / "app.py").write_text("def handle(x):\n    return x or 'both'\n")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "--no-edit")
    return repo


def test_history_names_both_sides_commits(merge_worktree):
    text = resolve_evidence.history(str(merge_worktree), ["src/app.py"])
    assert "src/app.py" in text
    assert "#101" in text
    assert "#202" in text


def test_history_labels_the_sides(merge_worktree):
    text = resolve_evidence.history(str(merge_worktree), ["src/app.py"])
    pr_side = text.index("#101")
    base_side = text.index("#202")
    assert "this PR" in text and "base" in text
    # The PR side is rendered before the base side for each path.
    assert pr_side < base_side


def test_history_untouched_path_yields_no_commits(merge_worktree):
    text = resolve_evidence.history(str(merge_worktree), ["tests/test_app.py"])
    assert "#101" not in text and "#202" not in text


def test_related_tests_finds_stem_and_content_matches(merge_worktree):
    tests = resolve_evidence.related_tests(str(merge_worktree), ["src/app.py"])
    assert "tests/test_app.py" in tests
    assert "tests/test_other.py" not in tests


def test_related_tests_includes_conflicted_test_files(merge_worktree):
    tests = resolve_evidence.related_tests(
        str(merge_worktree), ["tests/test_other.py"])
    assert "tests/test_other.py" in tests


def test_related_tests_caps_the_selection(merge_worktree):
    for i in range(15):
        (merge_worktree / "tests" / f"test_app_extra{i}.py").write_text(
            "from src import app\n")
    _git(merge_worktree, "add", "tests")
    _git(merge_worktree, "commit", "-m", "more tests")
    tests = resolve_evidence.related_tests(str(merge_worktree), ["src/app.py"])
    assert len(tests) <= resolve_evidence.MAX_RELATED_TESTS


def test_store_context_renders_summary_and_issues():
    rec = model.Pr(None, {
        "pr": 7,
        "summary": {"one_liner": "stops the flaky retry loop",
                    "primary_change": "backs off exponentially"},
        "issues": {"linked": [{"issue": 33, "how": "explicit",
                               "title": "Retry loop spins forever"}]},
    })
    text = resolve_evidence.store_context(rec)
    assert "stops the flaky retry loop" in text
    assert "#33" in text
    assert "Retry loop spins forever" in text


def test_store_context_empty_record_is_empty():
    assert resolve_evidence.store_context(model.Pr(None, {"pr": 8})) == ""


def test_related_tests_treats_the_stem_as_a_literal_not_a_regex(merge_worktree):
    (merge_worktree / "src" / "grid[2d].py").write_text("def cells(): pass\n")
    (merge_worktree / "tests" / "test_cells.py").write_text(
        "uses grid[2d] helpers\n")
    _git(merge_worktree, "add", ".")
    _git(merge_worktree, "commit", "-m", "add grid[2d]")
    tests = resolve_evidence.related_tests(str(merge_worktree), ["src/grid[2d].py"])
    assert "tests/test_cells.py" in tests


def test_related_tests_never_leaks_non_test_files(merge_worktree):
    # Every test file is itself conflicted, so the content search has no test
    # corpus left; it must return only the conflicted tests, not grep the tree.
    (merge_worktree / "src" / "uses_app.py").write_text("from src import app\n")
    _git(merge_worktree, "add", ".")
    _git(merge_worktree, "commit", "-m", "add consumer")
    tests = resolve_evidence.related_tests(
        str(merge_worktree),
        ["tests/test_app.py", "tests/test_other.py", "src/app.py"])
    assert "src/uses_app.py" not in tests
    assert all(t.startswith("tests/") for t in tests)
