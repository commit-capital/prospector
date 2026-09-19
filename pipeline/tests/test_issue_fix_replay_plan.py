from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from pipeline import profile
from pipeline.evals import issue_fix_replay as replay

_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}

_OPENED = datetime(2024, 3, 1, 12, 0, 0)
_BEFORE = datetime(2024, 2, 1, 9, 0, 0)


def _git(repo: Path, *args: str) -> str:
    env = {**{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}, **_GIT_ENV}
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=env).stdout


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def generic_profile(monkeypatch: pytest.MonkeyPatch) -> profile.RepoProfile:
    p = profile.RepoProfile()
    monkeypatch.setattr(profile, "active", lambda: p)
    return p


@pytest.fixture
def base_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A linear history whose commits each isolate one screening outcome, plus an
    off-branch commit that is not an ancestor of the pin."""
    repo = tmp_path / "base"
    repo.mkdir()
    _git(repo, "init", "-q")
    shas: dict[str, str] = {}
    shas["base"] = _commit(repo, {"src/app.py": "one\n", "README.md": "readme\n"}, "base")
    shas["ok"] = _commit(repo, {"src/app.py": "one\ntwo\n", "tests/test_app.py": "def test_x(): pass\n"}, "ok")
    shas["notest"] = _commit(repo, {"src/app.py": "one\ntwo\nthree\n"}, "notest")
    shas["manifest"] = _commit(
        repo,
        {"src/app.py": "one\ntwo\nfour\n", "tests/test_app.py": "def test_y(): pass\n",
         "package.json": '{"name": "x"}\n'},
        "manifest")
    shas["biglines"] = _commit(
        repo,
        {"src/big.py": "".join(f"row{i}\n" for i in range(401)),
         "tests/test_app.py": "def test_z(): pass\n"},
        "biglines")
    shas["bigfiles"] = _commit(
        repo,
        {**{f"src/mod{i}.py": f"m{i}\n" for i in range(10)},
         "tests/test_app.py": "def test_w(): pass\n"},
        "bigfiles")
    shas["onlytest"] = _commit(repo, {"tests/test_only.py": "def test_o(): pass\n"}, "onlytest")
    shas["pin"] = _commit(repo, {"README.md": "readme\nmore\n"}, "pin")
    _git(repo, "checkout", "-q", "-b", "off", shas["base"])
    shas["off"] = _commit(repo, {"src/app.py": "one\noff\n", "tests/test_app.py": "def test_o(): pass\n"}, "off")
    return repo, shas


def _pr(number: int, merge_sha: str, *, merged: bool = True, author: str = "alice") -> replay.ClosingPr:
    return replay.ClosingPr(number=number, merged=merged, merge_sha=merge_sha,
                            author=author, opened_at=_OPENED)


def _candidate(merge_sha: str, **over: object) -> replay.Candidate:
    fields: dict[str, object] = dict(
        issue=7, closed_completed=True, closing_prs=[_pr(42, merge_sha)],
        reporter="bob", created_at=_BEFORE, updated_at=_BEFORE,
        report_title="App crashes on start", report_body="Steps: run it and it dies.")
    fields.update(over)
    return replay.Candidate(**fields)  # type: ignore[arg-type]


def _screen(repo: Path, shas: dict[str, str], cand: replay.Candidate, p: profile.RepoProfile):
    return replay.screen(cand, base_clone=repo, pin_sha=shas["pin"], profile=p)


def test_passing_candidate_yields_instance(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    inst, reason = _screen(repo, shas, _candidate(shas["ok"]), generic_profile)
    assert reason is None
    assert inst is not None
    assert inst.issue == 7
    assert inst.pr == 42
    assert inst.merge_sha == shas["ok"]
    assert inst.test_files == ["tests/test_app.py"]
    assert inst.nontest_files == ["src/app.py"]
    assert inst.report_title == "App crashes on start"
    assert "diff --git" in inst.landed_diff


def test_r1_zero_merged_closers_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["ok"], closing_prs=[_pr(42, shas["ok"], merged=False)])
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "no-merged-closing-pr"


def test_r1_two_merged_closers_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["ok"], closing_prs=[_pr(42, shas["ok"]), _pr(43, shas["notest"])])
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "multiple-merged-closing-prs"


def test_r1_not_completed_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    inst, reason = _screen(repo, shas, _candidate(shas["ok"], closed_completed=False), generic_profile)
    assert inst is None
    assert reason == "not-closed-completed"


def test_r2_merge_not_ancestor_of_pin_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["off"], closing_prs=[_pr(42, shas["off"])])
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "merge-not-ancestor-of-pin"


def test_r3_no_test_file_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["notest"], closing_prs=[_pr(42, shas["notest"])])
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "no-test-file"


def test_r3_dependency_manifest_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["manifest"], closing_prs=[_pr(42, shas["manifest"])])
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "touches-dependency-manifest"


def test_r3_too_many_nontest_lines_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["biglines"], closing_prs=[_pr(42, shas["biglines"])])
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "too-many-nontest-lines"


def test_r3_too_many_files_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["bigfiles"], closing_prs=[_pr(42, shas["bigfiles"])])
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "too-many-files"


def test_r4_issue_edited_after_pr_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["ok"], updated_at=datetime(2024, 3, 2, 8, 0, 0))
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "issue-edited-after-pr"


def test_r4_report_names_pr_number_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["ok"], report_body="Duplicate of #42, see there.")
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "report-references-pr"


def test_r4_report_names_merge_sha_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["ok"], report_body=f"Fixed by {shas['ok'][:9]} already.")
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "report-references-pr"


def test_r5_bot_author_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["ok"], closing_prs=[_pr(42, shas["ok"], author="renovate[bot]")])
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "bot-or-lane-author"


def test_r5_lane_reporter_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    inst, reason = replay.screen(_candidate(shas["ok"], reporter="commitperclip-bot"),
                                 base_clone=repo, pin_sha=shas["pin"], profile=generic_profile,
                                 lane_logins=frozenset({"commitperclip-bot"}))
    assert inst is None
    assert reason == "bot-or-lane-author"


def _deps_repo(tmp_path: Path, name: str, root: str, workspace: str | None = None) -> tuple[Path, str]:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    files = {"package.json": root, "src/app.py": "x\n"}
    if workspace is not None:
        files["packages/foo/package.json"] = workspace
    sha = _commit(repo, files, "deps")
    return repo, sha


def test_dep_declarations_merges_every_workspace_manifest(tmp_path, generic_profile) -> None:
    repo, sha = _deps_repo(
        tmp_path, "deps",
        root='{"dependencies": {"left-pad": "^1.0.0"}, "devDependencies": {"jest": "^29"}}\n',
        workspace='{"dependencies": {"lodash": "^4"}, "peerDependencies": {"react": "^18"}}\n')
    assert replay.dep_declarations(repo, sha, generic_profile) == {
        "jest": "^29", "left-pad": "^1.0.0", "lodash": "^4", "react": "^18"}


def test_dep_declarations_ignores_non_json_and_object_values(tmp_path, generic_profile) -> None:
    repo = tmp_path / "mixed"
    repo.mkdir()
    _git(repo, "init", "-q")
    sha = _commit(repo, {
        "package.json": '{"dependencies": {"a": "1"}}\n',
        "pyproject.toml": "[project]\nname = 'x'\n",
        "package-lock.json": '{"dependencies": {"a": {"version": "1"}}}\n'},
        "mixed")
    assert replay.dep_declarations(repo, sha, generic_profile) == {"a": "1"}


def test_group_by_deps_groups_equal_declarations(tmp_path, generic_profile) -> None:
    repo = tmp_path / "grp"
    repo.mkdir()
    _git(repo, "init", "-q")
    sha_a = _commit(repo, {"package.json": '{"dependencies": {"a": "1"}}\n', "src/app.py": "one\n"}, "a")
    sha_b = _commit(repo, {"src/app.py": "two\n"}, "b")
    sha_c = _commit(repo, {"package.json": '{"dependencies": {"a": "2"}}\n'}, "c")

    def _inst(merge_sha: str, issue: int) -> replay.Instance:
        return replay.Instance(issue=issue, pr=issue, merge_sha=merge_sha, landed_diff="",
                               test_files=[], nontest_files=[], report_title="", report_body="")

    groups = replay.group_by_deps(
        [_inst(sha_a, 1), _inst(sha_b, 2), _inst(sha_c, 3)], repo, generic_profile)
    sizes = sorted(len(v) for v in groups.values())
    assert sizes == [1, 2]
    members = {frozenset(i.issue for i in v) for v in groups.values()}
    assert frozenset({1, 2}) in members
    assert frozenset({3}) in members


def test_r3_no_nontest_file_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    cand = _candidate(shas["onlytest"], closing_prs=[_pr(42, shas["onlytest"])])
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "no-nontest-file"


def test_r4_issue_created_after_pr_discarded(base_repo, generic_profile) -> None:
    repo, shas = base_repo
    after = datetime(2024, 3, 2, 8, 0, 0)
    cand = _candidate(shas["ok"], created_at=after, updated_at=after)
    inst, reason = _screen(repo, shas, cand, generic_profile)
    assert inst is None
    assert reason == "issue-created-after-pr"


def test_landed_diff_uses_the_first_parent_for_a_two_parent_merge(tmp_path, generic_profile) -> None:
    # A real "Merge pull request" commit: git show prints an empty combined diff,
    # so screen must read the diff against the merge's first parent instead.
    repo = tmp_path / "merge"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, {"src/app.py": "one\n", "README.md": "r\n"}, "base")
    default = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, {"src/app.py": "one\ntwo\n", "tests/test_app.py": "def test_x(): pass\n"}, "feature")
    _git(repo, "checkout", "-q", default)
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #42")
    merge = _git(repo, "rev-parse", "HEAD").strip()
    pin = _commit(repo, {"README.md": "r\nmore\n"}, "pin")
    assert _git(repo, "show", merge).count("diff --git") == 0  # git show is empty for the merge

    inst, reason = replay.screen(_candidate(merge, closing_prs=[_pr(42, merge)]),
                                 base_clone=repo, pin_sha=pin, profile=generic_profile)
    assert reason is None
    assert inst is not None
    assert inst.test_files == ["tests/test_app.py"]
    assert inst.nontest_files == ["src/app.py"]
