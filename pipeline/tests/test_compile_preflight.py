"""The merge-time compile preflight: the sandbox phase contract and the host
driver that runs (current default-branch tree + PR diff + profile compile
command) and records the container exit as the verdict."""
import re
from pathlib import Path

import pytest

from pipeline import compile_preflight, profile, verify_driver

SANDBOX = Path(__file__).resolve().parents[2] / "sandbox"

CONFIGURED = profile.parse_profile(
    {"version": 1, "verify": {"compile_cmd": "pnpm -r typecheck"}}, "t")

SRC_DIFF = """diff --git a/src/app.ts b/src/app.ts
index 1111111..2222222 100644
--- a/src/app.ts
+++ b/src/app.ts
@@ -1,1 +1,2 @@
 line
+added
"""

DEPS_DIFF = """diff --git a/package.json b/package.json
index 1111111..2222222 100644
--- a/package.json
+++ b/package.json
@@ -1,1 +1,2 @@
 {}
+x
"""

BASE = "f" * 40


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(profile, "active", lambda: CONFIGURED)


@pytest.fixture
def no_sandbox_calls(monkeypatch):
    """Fail the test if any docker-reaching seam is hit."""
    def boom(*a, **k):
        raise AssertionError("sandbox reached")
    monkeypatch.setattr(verify_driver, "run_phase", boom)
    monkeypatch.setattr(verify_driver, "build_base_image", boom)
    monkeypatch.setattr(verify_driver, "image_exists", boom)


def _patch_file(tmp_path, text):
    p = tmp_path / "h.patch"
    p.write_text(text)
    return p


class TestRunForMerge:
    def test_unconfigured_profile_means_no_preflight(self, monkeypatch, no_sandbox_calls):
        monkeypatch.setattr(profile, "active", lambda: profile.GENERIC)
        assert compile_preflight.run_for_merge(1, "a" * 40) is None

    def test_blank_head_is_refused(self, configured, no_sandbox_calls):
        res = compile_preflight.run_for_merge(1, "")
        assert res is not None and "head" in res["refused"]

    def test_empty_diff_is_refused(self, configured, no_sandbox_calls, monkeypatch, tmp_path):
        monkeypatch.setattr(verify_driver, "fetch_patch",
                            lambda pr, head: _patch_file(tmp_path, "not a diff"))
        res = compile_preflight.run_for_merge(1, "a" * 40)
        assert res is not None and "unparsable" in res["refused"]

    def test_deps_touching_diff_is_refused(self, configured, no_sandbox_calls,
                                           monkeypatch, tmp_path):
        monkeypatch.setattr(verify_driver, "fetch_patch",
                            lambda pr, head: _patch_file(tmp_path, DEPS_DIFF))
        res = compile_preflight.run_for_merge(1, "a" * 40)
        assert res is not None and "dependency manifests" in res["refused"]

    def test_clean_run_records_exit_and_base(self, configured, monkeypatch, tmp_path):
        calls: list = []
        patch = _patch_file(tmp_path, SRC_DIFF)
        monkeypatch.setattr(verify_driver, "fetch_patch", lambda pr, head: patch)
        monkeypatch.setattr(verify_driver, "resolve_base_sha", lambda: BASE)
        monkeypatch.setattr(verify_driver, "image_exists", lambda tag: True)
        monkeypatch.setattr(verify_driver, "build_base_image",
                            lambda sha, *, tier: pytest.fail("built despite image"))

        def fake_run(phase, image, **kw):
            calls.append((phase, image, kw))
            return 0, "ok"
        monkeypatch.setattr(verify_driver, "run_phase", fake_run)
        res = compile_preflight.run_for_merge(7, "a" * 40)
        assert res is not None
        assert res["exit"] == 0 and res["base_sha"] == BASE
        assert res["cmd"] == "pnpm -r typecheck"
        assert "error" not in res and "refused" not in res
        phase, image, kw = calls[0]
        assert phase == "compile"
        assert image == verify_driver.base_image_tag(BASE, 1)
        assert kw["tier"] == 1 and kw["test_cmd"] == "pnpm -r typecheck"
        assert kw["patch"] == patch and kw["base_sha"] == BASE

    def test_missing_image_is_built_first(self, configured, monkeypatch, tmp_path):
        order: list = []
        monkeypatch.setattr(verify_driver, "fetch_patch",
                            lambda pr, head: _patch_file(tmp_path, SRC_DIFF))
        monkeypatch.setattr(verify_driver, "resolve_base_sha", lambda: BASE)
        monkeypatch.setattr(verify_driver, "image_exists", lambda tag: False)

        def fake_build(sha, *, tier):
            order.append("build")
            return verify_driver.base_image_tag(sha, tier)
        monkeypatch.setattr(verify_driver, "build_base_image", fake_build)
        monkeypatch.setattr(verify_driver, "run_phase",
                            lambda *a, **k: (order.append("run"), (0, ""))[1])
        res = compile_preflight.run_for_merge(7, "a" * 40)
        assert res is not None and res["exit"] == 0
        assert order == ["build", "run"]

    def test_compile_failure_extracts_error_excerpt(self, configured, monkeypatch, tmp_path):
        monkeypatch.setattr(verify_driver, "fetch_patch",
                            lambda pr, head: _patch_file(tmp_path, SRC_DIFF))
        monkeypatch.setattr(verify_driver, "resolve_base_sha", lambda: BASE)
        monkeypatch.setattr(verify_driver, "image_exists", lambda tag: True)
        tail = ("> tsc -b\nsome noise\n"
                "src/x.ts(3,1): error TS2739: Type is missing fields\n"
                "found 1 error\n")
        monkeypatch.setattr(verify_driver, "run_phase", lambda *a, **k: (20, tail))
        res = compile_preflight.run_for_merge(7, "a" * 40)
        assert res is not None and res["exit"] == 20
        assert "TS2739" in res["error_excerpt"]

    def test_resolve_failure_is_an_error_not_a_raise(self, configured, monkeypatch, tmp_path):
        monkeypatch.setattr(verify_driver, "fetch_patch",
                            lambda pr, head: _patch_file(tmp_path, SRC_DIFF))

        def boom():
            raise RuntimeError("cannot resolve default branch")
        monkeypatch.setattr(verify_driver, "resolve_base_sha", boom)
        monkeypatch.setattr(verify_driver, "run_phase",
                            lambda *a, **k: pytest.fail("ran despite no base"))
        res = compile_preflight.run_for_merge(7, "a" * 40)
        assert res is not None
        assert "RuntimeError" in res["error"] and "exit" not in res

    def test_every_record_carries_duration(self, configured, no_sandbox_calls):
        res = compile_preflight.run_for_merge(1, "")
        assert res is not None and isinstance(res["duration_s"], float)


class TestPhaseDeclared:
    """The host driver names the phase "compile"; both sandbox scripts must
    know it, or every preflight would exit 2 (bad --phase) — a non-sentinel
    the gate reads as an infrastructure block."""

    def test_run_phase_has_compile_arm(self):
        text = (SANDBOX / "run-phase.sh").read_text()
        assert re.search(r"^  compile\)$", text, re.MULTILINE)

    def test_launcher_allows_compile_phase(self):
        text = (SANDBOX / "sandbox-run.sh").read_text()
        m = re.search(r'case "\$PHASE" in ([a-z|-]+)\) ;;', text)
        assert m is not None and "compile" in m.group(1).split("|")

    def test_launcher_requires_a_patch_for_compile(self):
        text = (SANDBOX / "sandbox-run.sh").read_text()
        m = re.search(r"^  ([a-z|-]*green[a-z|-]*)\)$", text, re.MULTILINE)
        assert m is not None and "compile" in m.group(1).split("|")
