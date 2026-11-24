"""Reusing a base image the compile preflight already built once the default
branch moves: verify_driver.head_delta_base picks the image, and _delta_from
writes upstream's own diff from that image's commit to HEAD."""
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import profile, verify_driver as vd, verify_gc


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path, monkeypatch):
    """A repository whose checkout sits at `base`, with later commits up to
    `head` already in its object store, as a clone that fetched them has."""
    monkeypatch.setattr(profile, "active", lambda: profile.GENERIC)
    monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    base = _commit(src, {"src/app.ts": "one\n", "package.json": "{}\n"}, "base")
    mid = _commit(src, {"src/app.ts": "one\ntwo\n", "src/new.ts": "x\n"}, "mid")
    head = _commit(src, {".env": "SECRET=1\n", "src/app.ts": "one\ntwo\nthree\n"}, "head")
    _git(src, "checkout", "-q", "--detach", base)
    return src, base, mid, head


class TestDeltaFrom:
    def test_the_diff_carries_the_base_tree_to_head(self, history, tmp_path):
        src, base, _, head = history
        found = vd._delta_from(src, head)
        assert found is not None
        got_base, patch = found
        assert got_base == base and patch is not None
        tree = tmp_path / "tree"
        _git(src, "worktree", "add", "-q", "--detach", str(tree), base)
        _git(tree, "apply", str(patch))
        assert (tree / "src/app.ts").read_text() == "one\ntwo\nthree\n"
        assert (tree / "src/new.ts").read_text() == "x\n"

    def test_a_file_every_clone_is_scrubbed_of_is_left_out(self, history):
        src, _, _, head = history
        found = vd._delta_from(src, head)
        assert found is not None and found[1] is not None
        assert ".env" not in found[1].read_text()

    def test_a_manifest_change_on_the_way_means_no_diff(self, history):
        src, base, _, _ = history
        _git(src, "checkout", "-q", "--detach", "HEAD@{1}")  # back to head's tip
        tip = _commit(src, {"package.json": '{"x": 1}\n'}, "deps")
        _git(src, "checkout", "-q", "--detach", base)
        assert vd._delta_from(src, tip) == (base, None)

    def test_a_base_too_far_behind_does_not_qualify(self, history, monkeypatch):
        src, _, _, head = history
        monkeypatch.setattr(vd, "DELTA_MAX_COMMITS", 1)
        assert vd._delta_from(src, head) is None

    def test_a_head_that_does_not_descend_from_the_base_does_not_qualify(self, history):
        src, _, mid, _ = history
        _git(src, "checkout", "-q", "--detach", mid)
        older = _git(src, "rev-parse", "HEAD~1")
        assert vd._delta_from(src, older) is None


class TestWhatRulesOutReuse:
    def _history_with(self, history, files):
        src, base, _, head = history
        _git(src, "checkout", "-q", "--detach", head)
        tip = _commit(src, files, "later")
        _git(src, "checkout", "-q", "--detach", base)
        return src, base, tip

    @pytest.mark.parametrize("path", ["patches/foo@1.0.0.patch", ".npmrc", ".pnpmfile.cjs",
                                      "rust-toolchain.toml", "server/prisma/schema.prisma"])
    def test_another_install_input(self, history, path):
        src, base, tip = self._history_with(history, {path: "changed\n"})
        assert vd._delta_from(src, tip) == (base, None)

    def test_a_file_a_lifecycle_script_runs(self, history):
        src, base, _, head = history
        _git(src, "checkout", "-q", "--detach", head)
        _commit(src, {"server/package.json":
                      '{"scripts": {"postinstall": "node scripts/assets.mjs"}}\n',
                      "server/scripts/assets.mjs": "one\n"}, "script")
        new_base = _git(src, "rev-parse", "HEAD")
        tip = _commit(src, {"server/scripts/assets.mjs": "two\n"}, "edit script")
        _git(src, "checkout", "-q", "--detach", new_base)
        assert vd._delta_from(src, tip) == (new_base, None)

    def test_key_material_the_build_gate_would_refuse(self, history):
        key = "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 64 + "\n"
        src, base, tip = self._history_with(history, {"deploy/id_rsa.txt": key})
        assert vd._delta_from(src, tip) == (base, None)

    def test_an_ordinary_source_change_is_reused(self, history):
        src, base, tip = self._history_with(history, {"server/scripts/other.mjs": "x\n"})
        found = vd._delta_from(src, tip)
        assert found is not None and found[0] == base and found[1] is not None


def _gen(sha12: str, tmp_path: Path, *, built: bool = True, hour: int = 0,
         tier: int = 1) -> verify_gc.Generation:
    ctx = tmp_path / "base" / sha12
    (ctx / "src").mkdir(parents=True)
    if built:
        (ctx / vd._BUILT_MARKER).touch()
    return verify_gc.Generation(
        sha12=sha12, images=(vd.base_image_tag(sha12, tier),), clone_dir=ctx,
        created=datetime(2026, 11, 24, hour, tzinfo=timezone.utc))


class TestHeadDeltaBase:
    @pytest.fixture
    def survey(self, monkeypatch, tmp_path):
        monkeypatch.setattr(verify_gc, "held_shas", lambda: frozenset())
        monkeypatch.setattr(verify_gc, "lock_path", lambda ctx: tmp_path / "locks" / ctx.name)
        monkeypatch.setattr(vd, "sandbox_image", lambda: "pr-verify:cur")
        monkeypatch.setattr(vd, "image_parent", lambda tag: "pr-verify:cur")

        def set_gens(gens):
            monkeypatch.setattr(verify_gc, "list_generations", lambda: gens)
        return set_gens

    def test_the_newest_finished_base_is_reused(self, survey, monkeypatch, tmp_path):
        survey([_gen("a" * 12, tmp_path, hour=1), _gen("b" * 12, tmp_path, hour=5)])
        patch = tmp_path / "d.patch"
        seen = []
        monkeypatch.setattr(vd, "_delta_from",
                            lambda src, head: seen.append(src.parent.name) or ("b" * 40, patch))
        with vd.head_delta_base("c" * 40, tier=1) as got:
            assert got == ("b" * 40, vd.base_image_tag("b" * 12, 1), patch)
        assert seen == ["b" * 12]

    def test_an_unfinished_or_held_or_outdated_base_is_skipped(self, survey, monkeypatch, tmp_path):
        survey([_gen("a" * 12, tmp_path, built=False, hour=9),
                _gen("b" * 12, tmp_path, hour=8),
                _gen("d" * 12, tmp_path, hour=7),
                _gen("e" * 12, tmp_path, hour=1)])
        monkeypatch.setattr(verify_gc, "held_shas", lambda: frozenset({"b" * 12}))
        monkeypatch.setattr(vd, "image_parent",
                            lambda tag: "pr-verify:old" if "d" * 12 in tag else "pr-verify:cur")
        patch = tmp_path / "d.patch"
        seen = []
        monkeypatch.setattr(vd, "_delta_from",
                            lambda src, head: seen.append(src.parent.name) or ("e" * 40, patch))
        with vd.head_delta_base("c" * 40, tier=1) as got:
            assert got is not None
        assert seen == ["e" * 12]

    def test_a_manifest_change_means_build(self, survey, monkeypatch, tmp_path):
        survey([_gen("a" * 12, tmp_path)])
        monkeypatch.setattr(vd, "_delta_from", lambda src, head: ("a" * 40, None))
        with vd.head_delta_base("c" * 40, tier=1) as got:
            assert got is None

    def test_a_failing_fetch_falls_through_to_the_next_base(self, survey, monkeypatch, tmp_path):
        survey([_gen("a" * 12, tmp_path, hour=1), _gen("b" * 12, tmp_path, hour=5)])
        patch = tmp_path / "d.patch"

        def delta(src, head):
            if src.parent.name == "b" * 12:
                raise subprocess.SubprocessError("fetch failed")
            return "a" * 40, patch
        monkeypatch.setattr(vd, "_delta_from", delta)
        with vd.head_delta_base("c" * 40, tier=1) as got:
            assert got is not None and got[0] == "a" * 40

    def test_no_docker_means_no_reuse(self, monkeypatch):
        def boom():
            raise FileNotFoundError("docker")
        monkeypatch.setattr(verify_gc, "list_generations", boom)
        with vd.head_delta_base("c" * 40, tier=1) as got:
            assert got is None

    def test_the_generation_stays_locked_while_in_use_and_the_diff_goes_after(
            self, survey, monkeypatch, tmp_path):
        gen = _gen("a" * 12, tmp_path)
        survey([gen])
        patch = tmp_path / "d.patch"
        patch.write_text("x")
        monkeypatch.setattr(vd, "_delta_from", lambda src, head: ("a" * 40, patch))
        assert gen.clone_dir is not None
        with vd.head_delta_base("c" * 40, tier=1) as got:
            assert got is not None
            # What a sweep does: take the lock without waiting, or skip.
            with verify_gc.generation_lock(gen.clone_dir, wait=False) as swept:
                assert not swept
        assert not patch.exists()
        with verify_gc.generation_lock(gen.clone_dir, wait=False) as swept:
            assert swept
