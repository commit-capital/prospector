"""Pinned-base proof primitives. The sandbox is mocked: what is pinned here is
which phases run, over which patch, and how their exits read."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import gates, prove, verify_driver as vd

BASE = prove.PinnedBase(sha="a" * 40, tier=1, image="pr-verify-base:aaaaaaaaaaaa-t1",
                        clone=Path("/nonexistent"))
TEST = ("diff --git a/x.test.ts b/x.test.ts\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/x.test.ts\n@@ -0,0 +1,1 @@\n+test\n")
FIX = "diff --git a/src/x.ts b/src/x.ts\n--- a/src/x.ts\n+++ b/src/x.ts\n@@ -1 +1 @@\n-a\n+b\n"


@pytest.fixture
def phases(monkeypatch, tmp_path):
    monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(prove, "SCRATCH", tmp_path / "scratch")
    calls: list[dict] = []
    exits: list[int] = []

    def fake(phase, image, **kw):
        calls.append({"phase": phase, "image": image, **kw})
        return exits.pop(0), f"tail of {phase}"

    monkeypatch.setattr(vd, "run_phase", fake)
    return calls, exits


def test_compose_concatenates_in_order_with_newlines(phases, tmp_path):
    out = prove.compose("issue-7", TEST.rstrip("\n"), None, FIX)
    assert out.read_text() == TEST + FIX
    assert out.parent == tmp_path / "scratch" / "issue-fix"


def test_compose_refuses_parts_that_touch_a_common_path(phases):
    with pytest.raises(ValueError, match="src/x.ts"):
        prove.compose("issue-7", FIX, FIX)


def test_red_confirms_only_after_a_failing_first_leg(phases):
    calls, exits = phases
    exits[:] = [gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL]
    legs = prove.red_legs(BASE, patch=prove.compose("i", TEST), test_cmd="t", label="issue-7")
    assert (legs["exit"], legs["exit_confirm"]) == (20, 20)
    assert [c["phase"] for c in calls] == ["red", "red"]
    assert calls[0]["base_sha"] == BASE.sha and calls[0]["head_sha"] == "issue-7"
    assert calls[0]["tier"] == 1 and calls[0]["image"] == BASE.image


def test_red_that_passes_runs_no_confirm(phases):
    calls, exits = phases
    exits[:] = [gates.SENTINEL_PASS]
    legs = prove.red_legs(BASE, patch=prove.compose("i", TEST), test_cmd="t", label="l")
    assert (legs["exit"], legs["exit_confirm"]) == (0, None) and len(calls) == 1


def test_green_confirms_only_after_a_passing_first_leg(phases):
    calls, exits = phases
    exits[:] = [gates.SENTINEL_PASS, gates.SENTINEL_PASS]
    legs = prove.green_legs(BASE, patch=prove.compose("i", TEST, FIX), test_cmd="t", label="l")
    assert (legs["exit"], legs["exit_confirm"]) == (0, 0)
    assert [c["phase"] for c in calls] == ["green", "green"]


def test_a_probe_failure_raises(phases):
    _, exits = phases
    exits[:] = [gates.SENTINEL_PROBE_FAIL]
    with pytest.raises(vd.ProbeFailure):
        prove.red_legs(BASE, patch=prove.compose("i", TEST), test_cmd="t", label="l")


def test_run_command_refuses_a_dependency_manifest(phases, tmp_path):
    p = tmp_path / "d.patch"
    p.write_text("diff --git a/package.json b/package.json\n--- a/package.json\n"
                 "+++ b/package.json\n@@ -1 +1 @@\n-a\n+b\n")
    rec = prove.run_command(BASE, p, "pnpm -r typecheck", phase="compile", label="l")
    assert "refused" in rec and "exit" not in rec


def test_run_command_reads_a_base_that_fails_the_compile_as_the_lanes_fault(phases, monkeypatch):
    _, exits = phases
    exits[:] = [gates.SENTINEL_TEST_FAIL]
    monkeypatch.setattr(vd, "base_command_failure", lambda image, cmd, run: "exit 20: TS2304")
    rec = prove.run_command(BASE, prove.compose("i", FIX), "pnpm -r typecheck",
                            phase="compile", label="l")
    assert rec["exit"] == 20 and rec["error_kind"] == "base-compile"


def test_pinned_raises_no_base_without_an_image(monkeypatch):
    monkeypatch.setattr(vd, "_pin", lambda store: ("b" * 40, 1))
    monkeypatch.setattr(vd, "daemon_available", lambda: True)
    monkeypatch.setattr(vd, "image_exists", lambda image: False)
    with pytest.raises(prove.NoBase, match="image"):
        prove.pinned(object())
