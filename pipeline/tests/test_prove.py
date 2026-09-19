"""Pinned-base proof primitives. The sandbox is mocked: what is pinned here is
which phases run, over which patch, and how their exits read."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline import gates, prove, verify_driver as vd

BASE = prove.PinnedBase(sha="a" * 40, tier=1, image="pr-verify-base:aaaaaaaaaaaa-t1",
                        clone=Path("/nonexistent"))
PIN_SHA = "b" * 40
TEST = ("diff --git a/x.test.ts b/x.test.ts\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/x.test.ts\n@@ -0,0 +1,1 @@\n+test\n")
FIX = "diff --git a/src/x.ts b/src/x.ts\n--- a/src/x.ts\n+++ b/src/x.ts\n@@ -1 +1 @@\n-a\n+b\n"


@pytest.fixture(autouse=True)
def _no_image_builds(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("the proof runs on the pinned base and builds no image")

    monkeypatch.setattr(vd, "build_base_image", boom)
    monkeypatch.setattr(vd, "resolve_base_sha", boom)


@pytest.fixture
def phases(monkeypatch, tmp_path):
    monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
    vd._base_command_failures.clear()
    calls: list[dict] = []
    exits: list[int] = []

    def fake(phase, image, **kw):
        calls.append({"phase": phase, "image": image, **kw})
        return exits.pop(0), f"tail of {phase}"

    monkeypatch.setattr(vd, "run_phase", fake)
    yield calls, exits
    vd._base_command_failures.clear()


@pytest.fixture
def pin(monkeypatch, tmp_path):
    monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(vd, "_pin", lambda store: (PIN_SHA, 2))
    monkeypatch.setattr(vd, "daemon_available", lambda: True)
    monkeypatch.setattr(vd, "image_exists", lambda image: True)
    vd.base_clone_dir(PIN_SHA).mkdir(parents=True)


def _compile_record(phases, exit_code: int) -> dict:
    _, exits = phases
    exits[:] = [exit_code]
    return prove.run_command(BASE, prove.compose("i", FIX), "pnpm -r typecheck",
                             phase="compile", label="l")


def test_compose_concatenates_in_order_with_newlines(phases, tmp_path):
    out = prove.compose("issue-7", TEST.rstrip("\n"), None, FIX)
    assert out.read_text() == TEST + FIX
    assert out.parent == tmp_path / "scratch" / "issue-fix"


def test_compose_refuses_parts_that_touch_a_common_path(phases):
    with pytest.raises(ValueError, match="src/x.ts"):
        prove.compose("issue-7", FIX, FIX)


def test_compose_names_one_file_per_composed_text(phases):
    once = prove.compose("issue-7", TEST, FIX)
    assert prove.compose("issue-7", TEST, FIX) == once
    other = prove.compose("issue-7", TEST)
    assert other != once
    assert {p.name for p in once.parent.iterdir()} == {once.name, other.name}


@pytest.mark.parametrize("label", ["issue/7", "..", "a/../b", "issue 7", "issue-7;rm"])
def test_compose_refuses_a_label_that_is_not_a_file_name(phases, label):
    with pytest.raises(ValueError, match="label"):
        prove.compose(label, TEST)


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
    assert rec["output_tail"] == ""


def test_run_command_reads_a_base_that_fails_the_compile_as_the_lanes_fault(phases):
    calls, exits = phases
    exits[:] = [gates.SENTINEL_TEST_FAIL, gates.SENTINEL_TEST_FAIL]
    rec = prove.run_command(BASE, prove.compose("i", FIX), "pnpm -r typecheck",
                            phase="compile", label="l")
    assert rec["exit"] == 20 and rec["error_kind"] == "base-compile"
    assert BASE.sha[:12] in rec["error"] and "tail of compile" in rec["error"]
    assert calls[1]["phase"] == "compile" and calls[1].get("pristine") is True
    assert "patch" not in calls[1] and calls[1]["tier"] == BASE.tier


def test_run_command_leaves_a_compile_failure_the_patchs_own_when_the_base_passes(phases):
    calls, exits = phases
    exits[:] = [gates.SENTINEL_TEST_FAIL, gates.SENTINEL_PASS]
    rec = prove.run_command(BASE, prove.compose("i", FIX), "pnpm -r typecheck",
                            phase="compile", label="l")
    assert rec["exit"] == 20 and rec["error_excerpt"] == "tail of compile"
    assert "error" not in rec and "error_kind" not in rec
    assert len(calls) == 2


def test_run_command_never_asks_the_pristine_base_about_a_green(phases):
    calls, exits = phases
    exits[:] = [gates.SENTINEL_TEST_FAIL]
    rec = prove.run_command(BASE, prove.compose("i", TEST, FIX), "pnpm -s test",
                            phase="green", label="l")
    assert len(calls) == 1
    assert rec["exit"] == 20 and rec["error_excerpt"] == "tail of green"
    assert "error" not in rec and "error_kind" not in rec


def test_a_patch_conflict_keeps_the_applys_own_words(phases):
    rec = _compile_record(phases, gates.SENTINEL_PATCH_CONFLICT)
    assert rec["exit"] == 30 and rec["error_excerpt"] == "tail of compile"
    assert "error" not in rec


def test_an_unreadable_patch_is_not_the_patchs_fault(phases):
    rec = _compile_record(phases, gates.SENTINEL_PATCH_UNREADABLE)
    assert rec["exit"] == 40
    assert "is not the patch's" in rec["error"] and "tail of compile" in rec["error"]


def test_a_non_sentinel_exit_names_the_exit(phases):
    rec = _compile_record(phases, 124)
    assert rec["exit"] == 124 and "exited 124" in rec["error"]


def test_run_command_records_an_unexpected_exception_and_raises_nothing(phases, monkeypatch):
    def boom(*a, **kw):
        raise vd.BuildFailure("the docker daemon went away")

    monkeypatch.setattr(vd, "run_phase", boom)
    rec = prove.run_command(BASE, prove.compose("i", FIX), "pnpm -r typecheck",
                            phase="compile", label="l")
    assert rec["error"] == "BuildFailure: the docker daemon went away"
    assert "exit" not in rec and rec["duration_s"] >= 0


def test_pinned_names_the_image_and_clone_its_sha_and_tier_derive(pin):
    base = prove.pinned(object())
    assert (base.sha, base.tier) == (PIN_SHA, 2)
    assert base.image == vd.base_image_tag(PIN_SHA, 2)
    assert base.clone == vd.base_clone_dir(PIN_SHA) and base.clone.is_dir()


def test_pinned_raises_no_base_without_a_pin(pin, monkeypatch):
    def unpinned(store):
        raise RuntimeError("no pinned base on this machine")

    monkeypatch.setattr(vd, "_pin", unpinned)
    with pytest.raises(prove.NoBase, match="no pinned base on this machine"):
        prove.pinned(object())


def test_pinned_raises_no_base_when_the_docker_daemon_is_down(pin, monkeypatch):
    monkeypatch.setattr(vd, "daemon_available", lambda: False)
    with pytest.raises(prove.NoBase, match="daemon"):
        prove.pinned(object())


def test_pinned_raises_no_base_without_an_image(pin, monkeypatch):
    monkeypatch.setattr(vd, "image_exists", lambda image: False)
    with pytest.raises(prove.NoBase, match="image"):
        prove.pinned(object())


def test_pinned_raises_no_base_without_a_clone(pin, monkeypatch, tmp_path):
    monkeypatch.setattr(vd, "SCRATCH", tmp_path / "elsewhere")
    with pytest.raises(prove.NoBase, match="clone"):
        prove.pinned(object())


HELD_SHA = "c" * 40


@pytest.fixture
def held(monkeypatch, tmp_path):
    clone = tmp_path / "held-clone"
    clone.mkdir()
    monkeypatch.setattr(vd, "base_image_tag", lambda sha, tier: f"pr-verify-base:{sha[:12]}-t{tier}")
    monkeypatch.setattr(vd, "base_clone_dir", lambda sha: clone)
    monkeypatch.setattr(vd, "daemon_available", lambda: True)
    monkeypatch.setattr(vd, "image_exists", lambda image: True)
    return clone


def test_held_names_the_image_and_clone_the_given_sha_and_tier_derive(held):
    base = prove.held(HELD_SHA, 1)
    assert (base.sha, base.tier) == (HELD_SHA, 1)
    assert base.image == vd.base_image_tag(HELD_SHA, 1)
    assert base.clone == held and base.clone.is_dir()


def test_held_raises_no_base_when_the_docker_daemon_is_down(held, monkeypatch):
    monkeypatch.setattr(vd, "daemon_available", lambda: False)
    with pytest.raises(prove.NoBase, match="daemon"):
        prove.held(HELD_SHA, 1)


def test_held_raises_no_base_without_an_image(held, monkeypatch):
    monkeypatch.setattr(vd, "image_exists", lambda image: False)
    with pytest.raises(prove.NoBase, match="image"):
        prove.held(HELD_SHA, 1)


def test_held_raises_no_base_without_a_clone(held, monkeypatch, tmp_path):
    monkeypatch.setattr(vd, "base_clone_dir", lambda sha: tmp_path / "gone")
    with pytest.raises(prove.NoBase, match="clone"):
        prove.held(HELD_SHA, 1)


# flatten: real git in tmp_path, mirroring issue_triage/tests/test_lane_tree.py's
# hermetic base fixture. Nothing here is mocked.

_FLATTEN_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_AUTHOR_NAME": "prospector", "GIT_AUTHOR_EMAIL": "prospector@localhost",
                    "GIT_COMMITTER_NAME": "prospector", "GIT_COMMITTER_EMAIL": "prospector@localhost"}


def _flatten_git(repo, *args, input=None):
    env = {**{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}, **_FLATTEN_GIT_ENV}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, timeout=60, env=env, input=input).stdout


def _flatten_base(tmp_path):
    base = tmp_path / "base-clone"
    (base / "src").mkdir(parents=True)
    (base / "src" / "x.ts").write_text("\n".join(f"line {n}" for n in range(1, 11)) + "\n")
    (base / ".git").mkdir()
    (base / ".git" / "marker").write_text("not part of the tree")
    return base


def _patch_from(tmp_path, name, base, edit):
    """A unified diff of `edit(work)` against a fresh one-commit copy of `base`,
    captured with real git so `git apply` accepts it."""
    work = tmp_path / name
    shutil.copytree(base, work, ignore=shutil.ignore_patterns(".git"))
    _flatten_git(work, "init", "-q")
    _flatten_git(work, "add", "-A")
    _flatten_git(work, "commit", "-q", "--no-gpg-sign", "-m", "base")
    edit(work)
    _flatten_git(work, "add", "-N", ".")
    return _flatten_git(work, "diff", "HEAD")


def _replace(path, old, new):
    path.write_text(path.read_text().replace(old, new))


@pytest.fixture
def flatten_scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")


def test_flatten_combines_two_patches_that_edit_the_same_file(flatten_scratch, tmp_path):
    base = _flatten_base(tmp_path)
    top = _patch_from(tmp_path, "top", base,
                      lambda w: _replace(w / "src" / "x.ts", "line 2", "line 2 top"))
    bottom = _patch_from(tmp_path, "bottom", base,
                         lambda w: _replace(w / "src" / "x.ts", "line 9", "line 9 bottom"))
    top_file = tmp_path / "top.patch"
    top_file.write_text(top)

    with pytest.raises(ValueError, match="src/x.ts"):
        prove.compose("issue-9", top, bottom)

    out = prove.flatten(base, top_file, bottom, label="issue-9")
    text = out.read_text()
    assert "line 2 top" in text and "line 9 bottom" in text


def test_flatten_combines_a_new_file_patch_with_an_edit_patch(flatten_scratch, tmp_path):
    base = _flatten_base(tmp_path)
    new_patch = _patch_from(tmp_path, "new", base,
                            lambda w: (w / "src" / "y.ts").write_text("export const y = 1;\n"))
    edit_patch = _patch_from(tmp_path, "edit", base,
                             lambda w: _replace(w / "src" / "x.ts", "line 5", "line 5 edited"))

    out = prove.flatten(base, new_patch, None, edit_patch, label="issue-10")
    text = out.read_text()
    assert "b/src/y.ts" in text and "export const y = 1;" in text
    assert "line 5 edited" in text


def test_flatten_raises_naming_the_index_of_a_patch_that_does_not_apply(flatten_scratch, tmp_path):
    base = _flatten_base(tmp_path)
    first = _patch_from(tmp_path, "first", base,
                        lambda w: _replace(w / "src" / "x.ts", "line 2", "line 2 first"))
    conflicting = _patch_from(tmp_path, "second", base,
                              lambda w: _replace(w / "src" / "x.ts", "line 2", "line 2 second"))

    with pytest.raises(ValueError, match="patch 1 does not apply"):
        prove.flatten(base, first, conflicting, label="issue-11")


def test_flatten_writes_under_the_scratch_dir_as_a_valid_unified_diff(flatten_scratch, tmp_path):
    base = _flatten_base(tmp_path)
    edit_patch = _patch_from(tmp_path, "edit", base,
                             lambda w: _replace(w / "src" / "x.ts", "line 3", "line 3 edited"))

    out = prove.flatten(base, edit_patch, label="issue-12")
    assert out.parent == tmp_path / "scratch" / "issue-fix"
    assert out.read_text().startswith("diff ")


def test_flatten_carries_full_blob_ids_and_binary_content(flatten_scratch, tmp_path):
    base = _flatten_base(tmp_path)
    (base / "logo.png").write_bytes(b"\x89PNG\x00old")
    binary = _patch_from(tmp_path, "bin", base,
                         lambda w: (w / "logo.png").write_bytes(b"\x89PNG\x00new"))
    binary = _flatten_git(tmp_path / "bin", "diff", "--binary", "HEAD")
    edit = _patch_from(tmp_path, "edit", base,
                       lambda w: _replace(w / "src" / "x.ts", "line 3", "line 3 edited"))

    text = prove.flatten(base, binary, edit, label="issue-12").read_text()

    assert "GIT binary patch" in text
    index_lines = [ln for ln in text.splitlines() if ln.startswith("index ")]
    assert index_lines and all(len(ln.split()[1].split("..")[0]) == 40 for ln in index_lines)
