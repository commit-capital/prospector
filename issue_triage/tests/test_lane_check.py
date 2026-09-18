import sys
from pathlib import Path

from issue_triage import lane_check
from pipeline import prove, settings


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


def test_records_path_sits_under_the_verify_scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "scratch"))
    assert lane_check.records_path(31, "fix") == (
        settings.verify_scratch() / "issue-fix" / "issue-31" / "fix.checks.jsonl")
