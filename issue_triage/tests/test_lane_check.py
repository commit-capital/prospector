import sys
from pathlib import Path

from issue_triage import lane_check
from pipeline import prove


def _base() -> prove.PinnedBase:
    return prove.PinnedBase(sha="a" * 40, tier=2, image="pr-verify-base:x",
                            clone=Path("/clones/a"))


def test_check_env_round_trips_through_base_from_env(tmp_path):
    base = _base()
    env = lane_check.check_env(issue=12, base=base, worktree=tmp_path / "wt",
                               records=tmp_path / "r.jsonl",
                               test_patch=tmp_path / "t.patch")
    assert lane_check.base_from_env(env) == base
    assert env["PROSPECTOR_ISSUE_CHECK_ISSUE"] == "12"
    assert env["PROSPECTOR_ISSUE_CHECK_WORKTREE"] == str(tmp_path / "wt")
    assert env["PROSPECTOR_ISSUE_CHECK_RECORDS"] == str(tmp_path / "r.jsonl")
    assert env["PROSPECTOR_ISSUE_CHECK_TEST_PATCH"] == str(tmp_path / "t.patch")
    assert env["PROSPECTOR_ISSUE_CHECK_MAX_RUNS"] == str(lane_check.MAX_RUNS)
    assert env["PROSPECTOR_PYTHON"] == sys.executable


def test_the_test_patch_key_is_absent_when_the_stage_has_no_frozen_tests(tmp_path):
    env = lane_check.check_env(issue=12, base=_base(), worktree=tmp_path / "wt",
                               records=tmp_path / "r.jsonl", test_patch=None)
    assert "PROSPECTOR_ISSUE_CHECK_TEST_PATCH" not in env


def test_records_path_sits_in_the_run_s_own_workdir(tmp_path):
    assert lane_check.records_path(tmp_path / "run-a", "fix") == (
        tmp_path / "run-a" / "fix.checks.jsonl")


def test_check_env_names_the_pre_patch_only_when_one_is_given(tmp_path):
    without = lane_check.check_env(issue=7, base=_base(), worktree=tmp_path / "wt",
                                   records=tmp_path / "r.jsonl", test_patch=None)
    assert "PROSPECTOR_ISSUE_CHECK_PRE_PATCH" not in without

    with_pre = lane_check.check_env(issue=7, base=_base(), worktree=tmp_path / "wt",
                                    records=tmp_path / "r.jsonl", test_patch=None,
                                    pre_patch=tmp_path / "pre.patch")
    assert with_pre["PROSPECTOR_ISSUE_CHECK_PRE_PATCH"] == str(tmp_path / "pre.patch")
