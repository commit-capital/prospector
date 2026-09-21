import subprocess

import pytest

from issue_triage import lane_tree

_PRE_PATCH = (
    "diff --git a/src/x.ts b/src/x.ts\n"
    "index e69de29..0000000 100644\n"
    "--- a/src/x.ts\n"
    "+++ b/src/x.ts\n"
    "@@ -1 +1 @@\n"
    "-export const x = 1;\n"
    "+export const x = 2;\n"
)


def _base(tmp_path):
    base = tmp_path / "base"
    (base / "src").mkdir(parents=True)
    (base / "src" / "x.ts").write_text("export const x = 1;\n")
    (base / ".git").mkdir()
    (base / ".git" / "secret-history").write_text("the fix lives here")
    return base


def _log(repo):
    return subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True,
                          text=True, check=True).stdout.splitlines()


def test_materialize_is_one_commit_without_the_bases_git_dir(tmp_path):
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "work" / "src")
    assert len(_log(repo)) == 1
    assert (repo / "src" / "x.ts").read_text() == "export const x = 1;\n"
    assert not (repo / ".git" / "secret-history").exists()


def test_materialize_commits_the_frozen_files(tmp_path):
    files = [{"path": "src/x.repro.test.ts", "contents": "test\n"}]
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "w", files)
    assert lane_tree.new_files(repo) == ([], [])
    assert (repo / "src" / "x.repro.test.ts").read_text() == "test\n"


def test_materialize_replaces_an_existing_destination(tmp_path):
    dest = tmp_path / "w"
    dest.mkdir()
    (dest / "stale").write_text("x")
    assert not (lane_tree.materialize(_base(tmp_path), dest) / "stale").exists()


def test_new_files_separates_additions_from_edits_to_committed_files(tmp_path):
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "w")
    (repo / "src" / "new.test.ts").write_text("t\n")
    (repo / "src" / "x.ts").write_text("export const x = 2;\n")
    untracked, other = lane_tree.new_files(repo)
    assert untracked == ["src/new.test.ts"] and other == ["src/x.ts"]


def test_read_files_refuses_a_symlink_and_non_utf8(tmp_path):
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "w")
    (repo / "link.test.ts").symlink_to(repo / "src" / "x.ts")
    (repo / "bin.test.ts").write_bytes(b"\xff\xfe\x00")
    assert lane_tree.read_files(repo, ["link.test.ts"])[1] == "not a regular file: link.test.ts"
    assert lane_tree.read_files(repo, ["bin.test.ts"])[1] == "not UTF-8 text: bin.test.ts"
    files, why = lane_tree.read_files(repo, ["src/x.ts"])
    assert why is None and files == [{"path": "src/x.ts", "contents": "export const x = 1;\n"}]


def test_authored_patch_is_the_edits_since_the_one_commit(tmp_path):
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "w")
    (repo / "src" / "x.ts").write_text("export const x = 2;\n")
    (repo / "src" / "y.ts").write_text("new\n")
    patch = lane_tree.authored_patch(repo)
    assert "-export const x = 1;" in patch and "+export const x = 2;" in patch
    assert "b/src/y.ts" in patch


def test_new_files_reads_a_rename_as_delete_plus_new(tmp_path):
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "w")
    subprocess.run(["git", "-C", str(repo), "mv", "src/x.ts", "src/z.ts"],
                   check=True, capture_output=True, text=True)
    added, other = lane_tree.new_files(repo)
    # The destination is an addition; the source the commit still holds is not,
    # so a rename always leaves an entry the caller refuses.
    assert added == ["src/z.ts"] and other == ["src/x.ts"]


def test_new_files_counts_an_intent_to_add_file_as_an_addition(tmp_path):
    # The lane's check tool stages the agent's new files intent-to-add so its
    # diff carries them; porcelain then calls them added, not untracked.
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "w")
    (repo / "src" / "new.test.ts").write_text("t\n")
    subprocess.run(["git", "-C", str(repo), "add", "-N", "."],
                   check=True, capture_output=True, text=True)
    added, other = lane_tree.new_files(repo)
    assert added == ["src/new.test.ts"] and other == []


def test_materialize_ignores_ambient_git_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hooks = home / "hooks"
    hooks.mkdir(parents=True)
    sentinel = tmp_path / "hook-fired"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    hook.chmod(0o755)
    (home / ".gitconfig").write_text(
        f"[core]\n\thooksPath = {hooks}\n[user]\n\tname = EVIL\n\temail = evil@example.com\n")
    monkeypatch.setenv("HOME", str(home))
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "w")
    assert not sentinel.exists()
    who = subprocess.run(["git", "-C", str(repo), "log", "--format=%an <%ae>"],
                         check=True, capture_output=True, text=True).stdout.strip()
    assert who == "prospector <prospector@localhost>"


def test_read_files_returns_no_partial_contents_on_a_later_bad_file(tmp_path):
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "w")
    (repo / "bin.test.ts").write_bytes(b"\xff\xfe")
    files, why = lane_tree.read_files(repo, ["src/x.ts", "bin.test.ts"])
    assert files == [] and why == "not UTF-8 text: bin.test.ts"


def test_materialize_applies_a_pre_patch_before_committing(tmp_path):
    repo = lane_tree.materialize(_base(tmp_path), tmp_path / "w", pre_patch=_PRE_PATCH)
    assert len(_log(repo)) == 1
    assert (repo / "src" / "x.ts").read_text() == "export const x = 2;\n"
    assert lane_tree.new_files(repo) == ([], [])


def test_materialize_raises_on_a_pre_patch_that_does_not_apply(tmp_path):
    bad = _PRE_PATCH.replace("-export const x = 1;", "-export const x = 999;")
    with pytest.raises(ValueError, match="pre_patch does not apply"):
        lane_tree.materialize(_base(tmp_path), tmp_path / "w", pre_patch=bad)
