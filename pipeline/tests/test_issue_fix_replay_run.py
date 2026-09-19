"""The run phase: build tree(P), validate the known human fix (R6), and score a
lane run against the merged PR's own tests as a hidden oracle. Every verdict is
a host-observed sandbox exit, so the sandbox legs and the lane are mocked and
only the module's own wiring runs; `transform_to_p` alone touches real git."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import gates, profile, prove, verify_driver
from pipeline.evals import issue_fix_replay as replay

_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}

_LANDED_DIFF = (
    "diff --git a/tests/test_app.py b/tests/test_app.py\n"
    "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
    "@@ -0,0 +1,1 @@\n+assert compute() == 1\n"
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n+++ b/src/app.py\n"
    "@@ -1,1 +1,1 @@\n+def compute(): return 1\n")

# A lane patch whose test hunk is the reproduction file and whose fix hunk names
# no symbol the PR's test references.
_LANE_PATCH = (
    "diff --git a/src/bug.test.ts b/src/bug.test.ts\n"
    "--- a/src/bug.test.ts\n+++ b/src/bug.test.ts\n"
    "@@ -0,0 +1,1 @@\n+expect(fixme()).toBe(1)\n"
    "diff --git a/src/x.ts b/src/x.ts\n"
    "--- a/src/x.ts\n+++ b/src/x.ts\n"
    "@@ -1,1 +1,1 @@\n+export function fixme() { return 1 }\n")

FIX_ONLY = 'diff --git a/src/x.ts b/src/x.ts\n--- a/src/x.ts\n+++ b/src/x.ts\n@@ -1,1 +1,1 @@\n+export function fixme() { return 1 }\n'


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


def _legs(exit_: int | None, confirm: int | None) -> dict:
    return {"exit": exit_, "exit_confirm": confirm, "output_tail": "", "duration_s": 1.0}


_RED_2020 = _legs(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL)
_GREEN_00 = _legs(gates.SENTINEL_PASS, gates.SENTINEL_PASS)


@pytest.fixture
def generic_profile(monkeypatch: pytest.MonkeyPatch) -> profile.RepoProfile:
    p = profile.RepoProfile()
    monkeypatch.setattr(profile, "active", lambda: p)
    return p


@pytest.fixture(autouse=True)
def stub_derive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whole-file command that carries its files, so a leg fake can tell the
    oracle command (the PR's tests) from the lane's own."""
    monkeypatch.setattr(verify_driver, "derive_test_command",
                        lambda files: ("cmd:" + ",".join(files)) if files else None)


def _instance(**over: object) -> replay.Instance:
    fields: dict[str, object] = dict(
        issue=7, pr=42, merge_sha="m" * 40, landed_diff=_LANDED_DIFF,
        test_files=["tests/test_app.py"], nontest_files=["src/app.py"],
        report_title="Crash", report_body="It dies.")
    fields.update(over)
    return replay.Instance(**fields)  # type: ignore[arg-type]


def _base(tmp_path: Path) -> prove.PinnedBase:
    return prove.PinnedBase(sha="e" * 40, tier=1, image="pr-verify-base:e",
                            clone=tmp_path / "clone")


def _mock_legs(monkeypatch: pytest.MonkeyPatch, *, red: dict, lane_green: dict,
               oracle_green: dict) -> None:
    monkeypatch.setattr(prove, "flatten",
                        lambda base_clone, *parts, label: Path("/tmp/patch.diff"))
    monkeypatch.setattr(prove, "red_legs",
                        lambda base, *, patch, test_cmd, label: red)
    monkeypatch.setattr(
        prove, "green_legs",
        lambda base, *, patch, test_cmd, label: oracle_green if "test_app" in test_cmd
        else lane_green)


# --- validate_known_fix (R6) ------------------------------------------------


def test_r6_passes_when_the_known_fix_reproduces(tmp_path, generic_profile, monkeypatch) -> None:
    _mock_legs(monkeypatch, red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(), label="replay-7")
    assert (ok, reason) == (True, "")


def test_r6_red_not_red_fails(tmp_path, generic_profile, monkeypatch) -> None:
    # The known test passes on tree(P): the bug does not reproduce there.
    _mock_legs(monkeypatch, red=_legs(gates.SENTINEL_PASS, None),
               lane_green=_GREEN_00, oracle_green=_GREEN_00)
    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(), label="replay-7")
    assert (ok, reason) == (False, "red")


def test_r6_green_not_green_fails(tmp_path, generic_profile, monkeypatch) -> None:
    _mock_legs(monkeypatch, red=_RED_2020,
               lane_green=_legs(gates.SENTINEL_TEST_FAIL, None),
               oracle_green=_legs(gates.SENTINEL_TEST_FAIL, None))
    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(), label="replay-7")
    assert (ok, reason) == (False, "green")


def test_r6_sandbox_fault_retries(tmp_path, generic_profile, monkeypatch) -> None:
    _mock_legs(monkeypatch, red=_legs(124, None), lane_green=_GREEN_00, oracle_green=_GREEN_00)
    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(), label="replay-7")
    assert (ok, reason) == (False, "sandbox")


def test_r6_probe_failure_is_a_sandbox_fault(tmp_path, generic_profile, monkeypatch) -> None:
    monkeypatch.setattr(prove, "flatten",
                        lambda base_clone, *parts, label: Path("/tmp/p.diff"))

    def raise_probe(base, *, patch, test_cmd, label):
        raise verify_driver.ProbeFailure("isolation unproven")

    monkeypatch.setattr(prove, "red_legs", raise_probe)
    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(), label="replay-7")
    assert (ok, reason) == (False, "sandbox")


# --- transform_to_p (real git) ----------------------------------------------


def test_transform_to_p_drops_dependency_manifest_sections(tmp_path, generic_profile) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    base = _commit(repo, {"src/app.py": "one\n", "package.json": '{"name": "x"}\n'}, "base")
    default = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    # P edits both a source file and the dependency manifest.
    parent = _commit(
        repo,
        {"src/app.py": "one\ntwo\n",
         "package.json": '{"name": "x", "dependencies": {"left-pad": "^1"}}\n'},
        "P")
    _git(repo, "checkout", "-q", "-b", "side", base)
    _commit(repo, {"src/other.py": "s\n"}, "side")
    _git(repo, "checkout", "-q", default)
    _git(repo, "merge", "-q", "--no-ff", "side", "-m", "Merge #42")
    merge = _git(repo, "rev-parse", "HEAD").strip()
    assert _git(repo, "rev-parse", "HEAD^1").strip() == parent  # first parent is P

    transform = replay.transform_to_p(repo, merge, base, generic_profile)
    assert "src/app.py" in transform
    assert "package.json" not in transform
    assert "src/other.py" not in transform  # reached only via the merge's second parent


# --- score ------------------------------------------------------------------


def _lane(ending: str, *, patch: str = _LANE_PATCH, reviews: list[dict] | None = None,
          repro_files: list[str] | None = None, outcome: str = "reproduced",
          agent_runs: int = 3) -> SimpleNamespace:
    files = [{"path": p} for p in (repro_files if repro_files is not None else ["src/bug.test.ts"])]
    return SimpleNamespace(
        ending=ending, agent_runs=agent_runs,
        reproduction={"outcome": outcome, "files": files},
        result={"patch": patch,
                "reviews": reviews if reviews is not None else [{"verdict": "safe"},
                                                                {"verdict": "safe"}]})


def _score(tmp_path, monkeypatch, lane: SimpleNamespace, *, red: dict, lane_green: dict,
           oracle_green: dict) -> tuple[dict, dict]:
    _mock_legs(monkeypatch, red=red, lane_green=lane_green, oracle_green=oracle_green)
    runs: dict = {}
    rec = replay.score(_instance(), lane, runs, base=_base(tmp_path), pre_patch="PRE",
                       label="replay-7")
    return rec, runs


def test_score_fixed_and_oracle_passing(tmp_path, generic_profile, monkeypatch) -> None:
    rec, runs = _score(tmp_path, monkeypatch, _lane("fixed"),
                       red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    assert rec["reproduced"] is True
    assert rec["fixed"] is True
    assert rec["repro_valid"] is True
    assert rec["oracle_pass"] is True
    assert rec["false_accept"] is False
    assert rec["false_reject"] is False
    assert rec["test_tamper"] is False
    assert rec["localized"] is False
    assert rec["agent_runs"] == 3
    assert runs["oracle_pass"] == _GREEN_00  # the extra leg is exposed


def test_score_fixed_but_oracle_failing_is_false_accept(tmp_path, generic_profile,
                                                        monkeypatch) -> None:
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed"),
                    red=_RED_2020, lane_green=_GREEN_00,
                    oracle_green=_legs(gates.SENTINEL_TEST_FAIL, None))
    assert rec["fixed"] is True
    assert rec["oracle_pass"] is False
    assert rec["oracle_coupled"] is False
    assert rec["false_accept"] is True
    assert rec["false_reject"] is False


def test_score_reproduced_not_fixed_but_oracle_passing_is_false_reject(
        tmp_path, generic_profile, monkeypatch) -> None:
    lane = _lane("fix-rejected", reviews=[{"verdict": "unsafe"}])
    rec, _ = _score(tmp_path, monkeypatch, lane,
                    red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    assert rec["reproduced"] is True
    assert rec["fixed"] is False
    assert rec["oracle_pass"] is True
    assert rec["false_reject"] is True
    assert rec["false_accept"] is False


def test_score_oracle_coupled_suppresses_false_accept(tmp_path, generic_profile,
                                                      monkeypatch) -> None:
    # The lane fix adds `compute`, the very symbol the PR's test asserts on.
    coupled_patch = (
        "diff --git a/src/bug.test.ts b/src/bug.test.ts\n"
        "--- a/src/bug.test.ts\n+++ b/src/bug.test.ts\n"
        "@@ -0,0 +1,1 @@\n+expect(fixme()).toBe(1)\n"
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1,1 +1,1 @@\n+def compute(): return 1\n")
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed", patch=coupled_patch),
                    red=_RED_2020, lane_green=_GREEN_00,
                    oracle_green=_legs(gates.SENTINEL_TEST_FAIL, None))
    assert rec["oracle_pass"] is False
    assert rec["oracle_coupled"] is True
    assert rec["false_accept"] is False


def test_score_test_tamper_when_fix_touches_a_foreign_test(tmp_path, generic_profile,
                                                           monkeypatch) -> None:
    tamper_patch = _LANE_PATCH + (
        "diff --git a/tests/other.test.ts b/tests/other.test.ts\n"
        "--- a/tests/other.test.ts\n+++ b/tests/other.test.ts\n"
        "@@ -1,1 +1,1 @@\n+it('x', () => {})\n")
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed", patch=tamper_patch),
                    red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    assert rec["test_tamper"] is True


def test_score_localized_when_lane_touches_a_landed_fix_file(tmp_path, generic_profile,
                                                             monkeypatch) -> None:
    localized_patch = (
        "diff --git a/src/bug.test.ts b/src/bug.test.ts\n"
        "--- a/src/bug.test.ts\n+++ b/src/bug.test.ts\n"
        "@@ -0,0 +1,1 @@\n+expect(fixme()).toBe(1)\n"
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1,1 +1,1 @@\n+def compute(): return 2\n")
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed", patch=localized_patch),
                    red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    assert rec["localized"] is True


# --- run_instance -----------------------------------------------------------


def test_run_instance_scores_a_passing_run(tmp_path, generic_profile, monkeypatch) -> None:
    monkeypatch.setattr(replay, "transform_to_p",
                        lambda base_clone, merge_sha, base_sha, profile: "PRE")
    _mock_legs(monkeypatch, red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    seen: dict = {}

    def fake_run_lane(*, issue, title, body, base, pre_patch, workdir):
        seen.update(issue=issue, title=title, pre_patch=pre_patch, workdir=workdir)
        return _lane("fixed", agent_runs=5)

    rec = replay.run_instance(_instance(), base=_base(tmp_path), base_sha="e" * 40,
                              profile=generic_profile, workdir=tmp_path / "work",
                              run_lane=fake_run_lane)
    assert seen["issue"] == 7
    assert seen["pre_patch"] == "PRE"
    assert rec["ending"] == "fixed"
    assert rec["issue"] == 7 and rec["pr"] == 42
    assert rec["fixed"] is True and rec["oracle_pass"] is True
    assert rec["agent_runs"] == 5
    assert isinstance(rec["seconds"], float)
    assert "oracle_pass" in rec["oracle_runs"]


def test_run_instance_r6_failure_skips_the_lane(tmp_path, generic_profile, monkeypatch) -> None:
    monkeypatch.setattr(replay, "transform_to_p",
                        lambda base_clone, merge_sha, base_sha, profile: "PRE")
    _mock_legs(monkeypatch, red=_legs(gates.SENTINEL_PASS, None),
               lane_green=_GREEN_00, oracle_green=_GREEN_00)

    def fake_run_lane(*, issue, title, body, base, pre_patch, workdir):
        raise AssertionError("the lane must not run when R6 fails")

    rec = replay.run_instance(_instance(), base=_base(tmp_path), base_sha="e" * 40,
                              profile=generic_profile, workdir=tmp_path / "work",
                              run_lane=fake_run_lane)
    assert rec["ending"] == "r6-red"
    assert rec["reason"] == "red"
    assert "fixed" not in rec


# --- coverage: metric/R6 branches that need a discriminating test -------------


def test_score_reproduced_false_when_the_lane_did_not_reproduce(tmp_path, generic_profile,
                                                                monkeypatch) -> None:
    lane = _lane("not-reproduced", outcome="not-reproduced")
    rec, _ = _score(tmp_path, monkeypatch, lane,
                    red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    assert rec["reproduced"] is False


def test_score_repro_valid_false_when_a_leg_does_not_confirm(tmp_path, generic_profile,
                                                             monkeypatch) -> None:
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed"),
                    red=_RED_2020, lane_green=_legs(gates.SENTINEL_TEST_FAIL, None),
                    oracle_green=_GREEN_00)
    assert rec["repro_valid"] is False
    assert rec["oracle_pass"] is True


def test_score_repro_valid_false_when_the_lane_patch_has_no_test_hunk(tmp_path, generic_profile,
                                                                      monkeypatch) -> None:
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed", patch=FIX_ONLY),
                    red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    assert rec["repro_valid"] is False


def test_r6_no_oracle_when_the_pr_ships_no_derivable_test_command(tmp_path, generic_profile) -> None:
    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE",
                                            _instance(test_files=[]), label="replay-7")
    assert (ok, reason) == (False, "no-oracle")
