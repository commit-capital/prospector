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

_TEST_ONLY = (
    "diff --git a/src/bug.test.ts b/src/bug.test.ts\n"
    "--- a/src/bug.test.ts\n+++ b/src/bug.test.ts\n"
    "@@ -0,0 +1,1 @@\n+expect(fixme()).toBe(1)\n")


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
                        lambda base, *, patch, test_cmd, label, tail_bytes=0: red)
    monkeypatch.setattr(
        prove, "green_legs",
        lambda base, *, patch, test_cmd, label, tail_bytes=0:
        oracle_green if "test_app" in test_cmd else lane_green)


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

    def raise_probe(base, *, patch, test_cmd, label, tail_bytes=0):
        raise verify_driver.ProbeFailure("isolation unproven")

    monkeypatch.setattr(prove, "red_legs", raise_probe)
    legs: dict = {}
    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(),
                                           label="replay-7", legs=legs)
    assert (ok, reason) == (False, "sandbox")
    assert legs["probe"]["output_tail"] == "isolation unproven"


def _report(*names: str) -> str:
    """A runner's end-of-run failed-tests report, the shape parse_failed_tests reads."""
    lines = [f"Failed Tests {len(names)}"] + [f" FAIL  {n}" for n in names]
    return "\n".join(lines) + "\n"


def _dirty(exit_: int | None, confirm: int | None, *names: str) -> dict:
    return {"exit": exit_, "exit_confirm": confirm, "output_tail": _report(*names),
            "duration_s": 1.0}


# The PR's own test, and a neighbour in the same file the PR never touched.
_MINE = "app.test.ts > compute > returns 1"
_NEIGHBOUR = "app.test.ts > unrelated > times out waiting"


def test_r6_accepts_a_green_whose_failures_the_red_run_already_carried(
        tmp_path, generic_profile, monkeypatch) -> None:
    # The oracle runs whole files: a neighbour failing in the sandbox both with
    # and without the fix is contamination, not the fix failing.
    red = _dirty(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL, _MINE, _NEIGHBOUR)
    green = _dirty(gates.SENTINEL_TEST_FAIL, None, _NEIGHBOUR)
    _mock_legs(monkeypatch, red=red, lane_green=green, oracle_green=green)
    legs: dict = {}

    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(),
                                           label="replay-7", legs=legs)

    assert (ok, reason) == (True, "")
    assert legs["green_confirm"] is not None  # a dirty green earns a second container


def test_r6_rejects_a_dirty_green_a_second_container_does_not_repeat(
        tmp_path, generic_profile, monkeypatch) -> None:
    red = _dirty(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL, _MINE, _NEIGHBOUR)
    # The second container fails on a test the red run never carried, so its
    # failing set is no longer contained.
    greens = [_dirty(gates.SENTINEL_TEST_FAIL, None, _NEIGHBOUR),
              _dirty(gates.SENTINEL_TEST_FAIL, None, "app.test.ts > other > flaky")]
    monkeypatch.setattr(prove, "flatten", lambda base_clone, *parts, label: Path("/tmp/p"))
    monkeypatch.setattr(prove, "red_legs",
                        lambda base, *, patch, test_cmd, label, tail_bytes=0: red)
    monkeypatch.setattr(prove, "green_legs",
                        lambda base, *, patch, test_cmd, label, tail_bytes=0: greens.pop(0))

    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(),
                                           label="replay-7")

    assert (ok, reason) == (False, "green")


def test_r6_rejects_a_green_failing_a_test_the_pr_s_own_diff_names(
        tmp_path, generic_profile, monkeypatch) -> None:
    # compute() is named by the PR's test hunks, so its failure is the PR's own
    # test still failing — never contamination.
    mine = "tests/test_app.py > compute() == 1"
    red = _dirty(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL, mine, _NEIGHBOUR)
    green = _dirty(gates.SENTINEL_TEST_FAIL, None, mine)
    _mock_legs(monkeypatch, red=red, lane_green=green, oracle_green=green)

    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(),
                                           label="replay-7")

    assert (ok, reason) == (False, "green")


_NO_EXPORT = ("SyntaxError: The requested module '../services/issues.ts' does not "
              "provide an export named 'parseStatusFilter'\n")


def _unbound(exit_: int | None) -> dict:
    return {"exit": exit_, "exit_confirm": None, "output_tail": _NO_EXPORT, "duration_s": 1.0}


def test_r6_names_a_green_whose_suite_never_bound_apart_from_one_it_refused(
        tmp_path, generic_profile, monkeypatch) -> None:
    _mock_legs(monkeypatch, red=_RED_2020, lane_green=_GREEN_00,
               oracle_green=_unbound(gates.SENTINEL_TEST_FAIL))

    ok, reason = replay.validate_known_fix(_base(tmp_path), "PRE", _instance(),
                                           label="replay-7")

    assert (ok, reason) == (False, "unbound")


def test_score_withholds_a_false_accept_when_the_pr_s_tests_never_ran(
        tmp_path, generic_profile, monkeypatch) -> None:
    # The lane's fix kept the helper module-private, so the PR's tests cannot
    # import it: they judge neither the fix nor the PR.
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed"),
                    red=_RED_2020, lane_green=_GREEN_00,
                    oracle_green=_unbound(gates.SENTINEL_TEST_FAIL))

    assert rec["oracle_outcome"] == "unbound"
    assert rec["oracle_pass"] is None
    assert rec["false_accept"] is False


def test_score_reads_a_false_accept_when_the_tests_ran_and_refused_it(
        tmp_path, generic_profile, monkeypatch) -> None:
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed"),
                    red=_RED_2020, lane_green=_GREEN_00,
                    oracle_green=_dirty(gates.SENTINEL_TEST_FAIL, None,
                                        "app.test.ts > compute > returns 1"))

    assert rec["oracle_outcome"] == "fail"
    assert rec["oracle_pass"] is False
    assert rec["false_accept"] is True


def test_oracle_imports_pr_symbols_names_the_couplings(generic_profile) -> None:
    landed = (
        "diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n"
        "@@ -1,1 +1,3 @@\n+export function parseStatus(x: string) { return x; }\n"
        "+export const LIMIT = 5;\n"
        "diff --git a/src/app.test.ts b/src/app.test.ts\n"
        "--- a/src/app.test.ts\n+++ b/src/app.test.ts\n"
        "@@ -0,0 +1,2 @@\n+import { parseStatus } from \"./app.ts\";\n"
        "+expect(parseStatus(\"a\")).toBe(\"a\")\n")

    assert replay.oracle_imports_pr_symbols(landed) == ["parseStatus"]  # LIMIT is unused


def test_oracle_imports_pr_symbols_is_empty_for_a_behavioural_oracle(generic_profile) -> None:
    landed = (
        "diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n"
        "@@ -1,1 +1,1 @@\n+const fixed = true;\n"
        "diff --git a/src/app.test.ts b/src/app.test.ts\n"
        "--- a/src/app.test.ts\n+++ b/src/app.test.ts\n"
        "@@ -0,0 +1,2 @@\n+import request from \"supertest\";\n"
        "+expect(res.status).toBe(200)\n")

    assert replay.oracle_imports_pr_symbols(landed) == []


def test_fix_path_withheld_names_a_tier_0_path(generic_profile) -> None:
    landed = (
        "diff --git a/.github/workflows/release.yml b/.github/workflows/release.yml\n"
        "--- a/.github/workflows/release.yml\n+++ b/.github/workflows/release.yml\n"
        "@@ -1,1 +1,1 @@\n+  run: ship\n")

    why = replay.fix_path_withheld(landed)

    assert "tier-0" in why and "release.yml" in why


def test_fix_path_withheld_is_empty_for_a_path_the_lane_may_author(generic_profile) -> None:
    landed = (
        "diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n"
        "@@ -1,1 +1,1 @@\n+const fixed = true;\n")

    assert replay.fix_path_withheld(landed) == ""


def test_fix_path_withheld_refuses_a_test_only_fix(generic_profile) -> None:
    landed = (
        "diff --git a/src/app.test.ts b/src/app.test.ts\n"
        "--- a/src/app.test.ts\n+++ b/src/app.test.ts\n@@ -0,0 +1,1 @@\n+expect(1)\n")

    assert replay.fix_path_withheld(landed) == "its fix changes no non-test file"


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


def test_transform_to_p_applies_to_the_scrubbed_base(tmp_path, generic_profile) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "logo.png").write_bytes(b"\x89PNG\x00old")
    base = _commit(repo, {"src/app.py": "one\n", ".env.e2e.example": "A=1\n"}, "P")
    default = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "side", base)
    _commit(repo, {"src/other.py": "s\n"}, "side")
    _git(repo, "checkout", "-q", default)
    _git(repo, "merge", "-q", "--no-ff", "side", "-m", "Merge #42")
    merge = _git(repo, "rev-parse", "HEAD").strip()
    # The pin is later history: it edits a binary file and a file the scrub removes.
    (repo / "logo.png").write_bytes(b"\x89PNG\x00new")
    pin = _commit(repo, {"src/app.py": "one\ntwo\n", ".env.e2e.example": "A=2\n"}, "later")
    verify_driver.scrub_checkout(repo)

    transform = replay.transform_to_p(repo, merge, pin, generic_profile)

    assert "GIT binary patch" in transform
    assert ".env.e2e.example" not in transform
    subprocess.run(["git", "-C", str(repo), "apply", "--check", "-"], check=True,
                   input=transform, text=True, capture_output=True)


# --- score ------------------------------------------------------------------


def _lane(ending: str, *, patch: str = _LANE_PATCH, reviews: list[dict] | None = None,
          repro_files: list[str] | None = None, outcome: str = "reproduced",
          agent_runs: int = 3, detail: str = "") -> SimpleNamespace:
    files = [{"path": p} for p in (repro_files if repro_files is not None else ["src/bug.test.ts"])]
    return SimpleNamespace(
        ending=ending, detail=detail, agent_runs=agent_runs,
        reproduction={"outcome": outcome, "files": files},
        result={"patch": patch,
                "reviews": reviews if reviews is not None else [{"verdict": "safe"},
                                                                {"verdict": "safe"}]})


def _score(tmp_path, monkeypatch, lane: SimpleNamespace, *, red: dict, lane_green: dict,
           oracle_green: dict, r6_red: dict | None = None,
           judge_contract: replay.ContractJudge | None = None) -> tuple[dict, dict]:
    _mock_legs(monkeypatch, red=red, lane_green=lane_green, oracle_green=oracle_green)
    runs: dict = {}
    rec = replay.score(_instance(), lane, runs, base=_base(tmp_path), pre_patch="PRE",
                       label="replay-7", r6_red=r6_red, judge_contract=judge_contract)
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


def test_score_records_coupling_beside_a_false_accept(tmp_path, generic_profile,
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
    # Coupling is context for reading the row, not an exemption: reviewers
    # called this safe and the PR's own tests refuse it.
    assert rec["false_accept"] is True


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
    assert rec["detail"].startswith(f"red exit {gates.SENTINEL_PASS}/None")
    assert isinstance(rec["r6_seconds"], float)
    assert "fixed" not in rec


def test_run_instance_r6_sandbox_fault_carries_the_sandbox_error(tmp_path, generic_profile,
                                                                 monkeypatch) -> None:
    monkeypatch.setattr(replay, "transform_to_p",
                        lambda base_clone, merge_sha, base_sha, profile: "PRE")
    unreadable = {"exit": gates.SENTINEL_PATCH_UNREADABLE, "exit_confirm": None,
                  "output_tail": "Applying...\nthe patch could not be applied: error: short "
                                 "object ID 02b68fc is ambiguous\n",
                  "duration_s": 1.0}
    _mock_legs(monkeypatch, red=unreadable, lane_green=_GREEN_00, oracle_green=_GREEN_00)

    rec = replay.run_instance(_instance(), base=_base(tmp_path), base_sha="e" * 40,
                              profile=generic_profile, workdir=tmp_path / "work",
                              run_lane=lambda **kw: None)
    assert rec["ending"] == "r6-sandbox"
    assert rec["detail"].startswith(f"red exit {gates.SENTINEL_PATCH_UNREADABLE}/None")
    assert "short object ID 02b68fc is ambiguous" in rec["detail"]


def test_run_instance_faulted_lane_is_not_scored(tmp_path, generic_profile, monkeypatch) -> None:
    monkeypatch.setattr(replay, "transform_to_p",
                        lambda base_clone, merge_sha, base_sha, profile: "PRE")
    monkeypatch.setattr(replay, "validate_known_fix",
                        lambda base, pre_patch, instance, *, label, legs=None: (True, ""))
    touched: list[str] = []
    monkeypatch.setattr(replay, "score", lambda *a, **k: touched.append("score") or {})
    monkeypatch.setattr(prove, "green_legs",
                        lambda *a, **k: touched.append("green") or _GREEN_00)

    def fake_run_lane(*, issue, title, body, base, pre_patch, workdir):
        return _lane("sandbox", agent_runs=2, detail="sandbox could not boot")

    rec = replay.run_instance(_instance(), base=_base(tmp_path), base_sha="e" * 40,
                              profile=generic_profile, workdir=tmp_path / "work",
                              run_lane=fake_run_lane)
    assert rec["ending"] == "sandbox"
    assert rec["agent_runs"] == 2
    assert rec["detail"] == "sandbox could not boot"
    assert "oracle_pass" not in rec  # a faulted lane is not scored
    assert touched == []  # neither score nor its extra legs ran


def test_score_without_fix_hunks_reads_as_no_oracle_pass(tmp_path, generic_profile,
                                                         monkeypatch) -> None:
    # The lane wrote a test and no fix: the bug is still there, so the PR's own
    # test cannot pass and no sandbox leg is spent asking.
    rec, runs = _score(tmp_path, monkeypatch, _lane("fixed", patch=_TEST_ONLY),
                       red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    assert rec["oracle_pass"] is False
    assert "oracle_pass" not in runs


def test_score_accepts_an_oracle_green_contaminated_the_same_way_red_was(
        tmp_path, generic_profile, monkeypatch) -> None:
    # The oracle runs the PR's files whole: a neighbour failing there too is
    # contamination the R6 red leg already carried, not the lane's fix failing.
    r6_red = _dirty(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL,
                    "tests/test_app.py > compute() == 1", _NEIGHBOUR)
    dirty = _dirty(gates.SENTINEL_TEST_FAIL, None, _NEIGHBOUR)
    rec, runs = _score(tmp_path, monkeypatch, _lane("fixed"),
                       red=_RED_2020, lane_green=_GREEN_00, oracle_green=dirty,
                       r6_red=r6_red)
    assert rec["oracle_pass"] is True
    assert rec["false_accept"] is False
    assert "oracle_pass_confirm" in runs  # a second container repeated it


def test_score_without_an_r6_baseline_requires_a_clean_oracle_green(
        tmp_path, generic_profile, monkeypatch) -> None:
    dirty = _dirty(gates.SENTINEL_TEST_FAIL, None, _NEIGHBOUR)
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed"),
                    red=_RED_2020, lane_green=_GREEN_00, oracle_green=dirty)
    assert rec["oracle_pass"] is False


def test_run_instance_records_each_leg_and_what_the_lane_reported(
        tmp_path, generic_profile, monkeypatch) -> None:
    monkeypatch.setattr(replay, "transform_to_p",
                        lambda base_clone, merge_sha, base_sha, profile: "PRE")
    r6_red = _dirty(gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL,
                    "tests/test_app.py > compute() == 1", _NEIGHBOUR)

    def fake_r6(base, pre_patch, instance, *, label, legs=None):
        if legs is not None:
            legs["red"] = r6_red
        return True, ""

    monkeypatch.setattr(replay, "validate_known_fix", fake_r6)
    _mock_legs(monkeypatch, red=_RED_2020, lane_green=_GREEN_00,
               oracle_green=_dirty(gates.SENTINEL_TEST_FAIL, None, _NEIGHBOUR))

    def fake_run_lane(*, issue, title, body, base, pre_patch, workdir):
        return _lane("fixed", reviews=[{"verdict": "safe", "lens": "root-cause",
                                        "reason": "addresses the named cause"}])

    rec = replay.run_instance(_instance(), base=_base(tmp_path), base_sha="e" * 40,
                              profile=generic_profile, workdir=tmp_path / "work",
                              run_lane=fake_run_lane)

    assert rec["legs"]["r6:red"]["exit"] == gates.SENTINEL_TEST_FAIL
    assert rec["legs"]["oracle_pass"]["failing"] == [_NEIGHBOUR]
    assert rec["legs"]["oracle_pass"]["failing_parsed"] is True
    assert rec["lane"]["reviews"][0]["lens"] == "root-cause"
    assert rec["lane"]["patch"]["files"] == 2
    assert rec["lane"]["repro_outcome"] == "reproduced"


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


# --- who owns an oracle failure -----------------------------------------------


def _judged(contract: str, seen: list | None = None) -> replay.ContractJudge:
    def judge(instance: replay.Instance, failing: list[str] | None) -> dict:
        if seen is not None:
            seen.append(failing)
        return {"contract": contract, "tests": [], "reason": ""}
    return judge


def test_score_reads_a_failure_on_the_maintainers_contract_as_a_mismatch(
        tmp_path, generic_profile, monkeypatch) -> None:
    seen: list = []
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed"),
                    red=_RED_2020, lane_green=_GREEN_00,
                    oracle_green=_dirty(gates.SENTINEL_TEST_FAIL, None,
                                        "app.test.ts > list > returns 422"),
                    judge_contract=_judged("maintainer", seen))

    assert seen == [["app.test.ts > list > returns 422"]]
    assert rec["oracle_outcome"] == "fail"
    assert rec["contract_mismatch"] is True
    assert rec["false_accept"] is False


@pytest.mark.parametrize("contract", ["report", "unknown"])
def test_score_keeps_a_false_accept_the_report_owns_or_no_judge_can_place(
        tmp_path, generic_profile, monkeypatch, contract: str) -> None:
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed"),
                    red=_RED_2020, lane_green=_GREEN_00,
                    oracle_green=_dirty(gates.SENTINEL_TEST_FAIL, None,
                                        "app.test.ts > compute > returns 1"),
                    judge_contract=_judged(contract))

    assert rec["contract_mismatch"] is False
    assert rec["false_accept"] is True


def test_score_asks_no_judge_when_the_oracle_passes(tmp_path, generic_profile,
                                                    monkeypatch) -> None:
    seen: list = []
    rec, _ = _score(tmp_path, monkeypatch, _lane("fixed"),
                    red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00,
                    judge_contract=_judged("maintainer", seen))

    assert seen == []
    assert rec["oracle_contract"] is None
    assert rec["contract_mismatch"] is False


def test_run_instance_records_the_related_tests_the_lane_ran(
        tmp_path, generic_profile, monkeypatch) -> None:
    monkeypatch.setattr(replay, "transform_to_p",
                        lambda base_clone, merge_sha, base_sha, profile: "PRE")
    monkeypatch.setattr(replay, "validate_known_fix",
                        lambda base, pre_patch, instance, *, label, legs=None: (True, ""))
    _mock_legs(monkeypatch, red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)

    def fake_run_lane(*, issue, title, body, base, pre_patch, workdir):
        lane = _lane("fixed")
        lane.result["proof"] = {"related_tests": {
            "files": ["src/issues.test.ts"], "run": {"exit": gates.SENTINEL_PASS}}}
        return lane

    rec = replay.run_instance(_instance(), base=_base(tmp_path), base_sha="e" * 40,
                              profile=generic_profile, workdir=tmp_path / "work",
                              run_lane=fake_run_lane)

    assert rec["lane"]["related_tests"] == {"files": ["src/issues.test.ts"],
                                            "exit": gates.SENTINEL_PASS, "base_fails": False}

def test_score_reads_the_lane_s_preservation_tests_as_its_own(tmp_path, generic_profile,
                                                              monkeypatch) -> None:
    keep = ("diff --git a/src/keep.test.ts b/src/keep.test.ts\n"
            "--- a/src/keep.test.ts\n+++ b/src/keep.test.ts\n"
            "@@ -0,0 +1,1 @@\n+expect(fixme(2)).toBe(2)\n")
    lane = _lane("fixed", patch=keep + _LANE_PATCH)
    lane.reproduction["preserve"] = [{"path": "src/keep.test.ts"}]
    rec, _ = _score(tmp_path, monkeypatch, lane,
                    red=_RED_2020, lane_green=_GREEN_00, oracle_green=_GREEN_00)
    assert rec["test_tamper"] is False
