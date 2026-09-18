import subprocess

from issue_triage import lane_tree


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


def test_new_files_separates_untracked_from_edits_to_tracked_files(tmp_path):
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
    untracked, other = lane_tree.new_files(repo)
    assert untracked == [] and other == ["src/x.ts", "src/z.ts"]


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
