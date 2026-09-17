"""The agents' `git-read` CLI — read-only git inside one pinned worktree. Driven
end to end as a subprocess (how a headless agent invokes it) against real
repositories, so the tests assert what reaches the agent: nothing outside the
worktree is printed, and nothing anywhere is written."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GIT_READ = REPO_ROOT / "prospector_app" / "agent" / "git-read"
CANARY = "CANARY-OUTSIDE-THE-WORKTREE"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    root = (tmp_path / "wt").resolve()
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "first commit")
    (root / "a.txt").write_text("one\ntwo\n")
    _git(root, "commit", "-qam", "second commit")
    (root / "a.txt").write_text("one\ntwo\nuncommitted\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "canary.txt").write_text(CANARY + "\n")
    return root


def _run(worktree: Path | None, *args: str, cwd: Path | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", **(env or {})}
    if worktree is not None:
        full["PROSPECTOR_GIT_WORKTREE"] = str(worktree)
    return subprocess.run([sys.executable, str(GIT_READ), *args], env=full,
                          cwd=cwd or worktree, capture_output=True, text=True)


def _refused(r: subprocess.CompletedProcess[str]) -> bool:
    return r.returncode == 2 and CANARY not in r.stdout + r.stderr


def test_the_four_queries_read_the_pinned_worktree(wt):
    assert " M a.txt" in _run(wt, "status", "--short").stdout
    log = _run(wt, "log", "--oneline", "-n", "5").stdout
    assert "second commit" in log and "first commit" in log
    assert "+uncommitted" in _run(wt, "diff").stdout
    assert "+two" in _run(wt, "show", "HEAD").stdout
    assert _run(wt, "show", "HEAD~1:a.txt").stdout == "one\n"
    assert "+two" in _run(wt, "diff", "HEAD~1", "HEAD", "--", "a.txt").stdout


def test_the_worktree_comes_from_the_environment_never_the_cwd(wt, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert "second commit" in _run(wt, "log", "--oneline", cwd=other).stdout
    r = _run(None, "log", cwd=wt)
    assert r.returncode == 2 and "PROSPECTOR_GIT_WORKTREE" in r.stderr


@pytest.mark.parametrize("args", [
    ["diff", "--no-index", "../outside/canary.txt", "/dev/null"],
    ["diff", "--no-index", "--", "../outside/canary.txt", "a.txt"],
    ["diff", "--no-ind", "../outside/canary.txt", "a.txt"],
    ["diff", "../outside/canary.txt", "a.txt"],
    ["diff", "--", "../outside/canary.txt", "a.txt"],
    ["diff", "a.txt", "sub/../../outside/canary.txt"],
])
def test_no_form_of_diff_reads_a_file_outside_the_worktree(wt, args):
    assert _refused(_run(wt, *args))


def test_an_absolute_path_is_refused_even_inside_the_worktree(wt, tmp_path):
    assert _refused(_run(wt, "diff", str(tmp_path / "outside" / "canary.txt"), "a.txt"))
    assert _refused(_run(wt, "log", "--", str(wt / "a.txt")))


def test_a_symlink_out_of_the_worktree_is_not_followed(wt, tmp_path):
    (wt / "link").symlink_to(tmp_path / "outside")
    assert _refused(_run(wt, "diff", "link/canary.txt", "a.txt"))
    assert _refused(_run(wt, "diff", "--", "link/canary.txt"))


@pytest.mark.parametrize("sub", ["log", "show", "diff"])
def test_output_cannot_be_written_to_a_file(wt, tmp_path, sub):
    target = tmp_path / "outside" / "written.txt"
    for flag in (f"--output={target}", "--output", f"--outpu={target}"):
        assert _run(wt, sub, flag).returncode == 2
    assert not target.exists()


@pytest.mark.parametrize("args", [
    ["diff", "--ext-diff"], ["show", "--textconv", "HEAD"], ["diff", "-O/etc/hosts"],
    ["log", "--show-signature"], ["log", "-L1,2:a.txt"], ["status", "--no-index"],
    ["grep", "one"], ["-C", "/", "log"], ["log", "-n", "five"], ["config", "--list"],
])
def test_an_option_or_subcommand_off_the_list_is_refused(wt, args):
    r = _run(wt, *args)
    assert r.returncode == 2 and "git-read" in r.stderr


def test_repository_configuration_runs_no_program(wt, tmp_path):
    marker = tmp_path / "ran"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    for key in ("core.fsmonitor", "diff.external", "diff.evil.textconv",
                "diff.evil.command", "core.pager"):
        _git(wt, "config", key, str(script))
    _git(wt, "config", "log.showSignature", "true")
    (wt / ".gitattributes").write_text("* diff=evil\n")
    for args in (["status"], ["diff"], ["diff", "HEAD~1"], ["show", "HEAD"],
                 ["log", "-p", "-n", "2"]):
        _run(wt, *args)
    assert not marker.exists()


def test_the_callers_git_environment_cannot_aim_the_tool_elsewhere(wt, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    (other / "secret.txt").write_text(CANARY + "\n")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", f"{CANARY} commit")
    r = _run(wt, "log", "-p", env={"GIT_DIR": str(other / ".git"),
                                   "GIT_WORK_TREE": str(other)})
    assert "second commit" in r.stdout and CANARY not in r.stdout


def test_a_pinned_directory_that_is_no_repository_never_reads_an_enclosing_one(wt):
    nested = wt / "nested"
    nested.mkdir()
    r = _run(nested, "log", "--oneline")
    assert r.returncode != 0 and "second commit" not in r.stdout


def test_status_leaves_the_index_untouched(wt):
    index = wt / ".git" / "index"
    before = index.stat().st_mtime_ns
    assert _run(wt, "status").returncode == 0
    assert index.stat().st_mtime_ns == before


def test_conflict_stages_and_both_sides_are_readable(wt):
    _git(wt, "checkout", "-q", "--", "a.txt")
    _git(wt, "checkout", "-qb", "side", "HEAD~1")
    (wt / "a.txt").write_text("one\nside\n")
    _git(wt, "commit", "-qam", "side commit")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "merge", "main"], cwd=wt, capture_output=True)
    assert _run(wt, "show", ":2:a.txt").stdout == "one\nside\n"
    assert _run(wt, "show", ":3:a.txt").stdout == "one\ntwo\n"
    assert "<<<<<<<" in _run(wt, "diff").stdout
    merge_log = _run(wt, "log", "--merge", "--oneline", "--left-right").stdout
    assert "side commit" in merge_log and "second commit" in merge_log
    assert _run(wt, "diff", "--ours").returncode == 0
