"""Verify-sandbox base GC: the retention policy, the Docker/filesystem survey
that feeds it, and collect's never-raise contract.

`plan` is pure, so the policy — which generation survives a re-pin — is driven
here with no Docker and no filesystem. The load-bearing property is that the
pinned SHA is never in a delete set, whatever the timestamps say."""
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import verify_gc as gc

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


# Clone mtimes sit well before every `docker image ls` stamp these tests feed
# in, so a generation that has both artifacts always takes its rank from the
# image — the clone is only the tiebreaker for a clone-only generation.
CLONE_EPOCH = datetime(2026, 8, 1, tzinfo=timezone.utc)


def clone_dirs(root: Path, *shas: str) -> dict[str, Path]:
    """Clone directories for `shas` under `root`, aged oldest-first in the order
    given — so which generation the retention rule ranks newest is fixed by the
    fixture rather than by how fast the test happened to run."""
    out: dict[str, Path] = {}
    for i, sha in enumerate(shas):
        d = root / "base" / (sha * 12)
        d.mkdir(parents=True)
        os.utime(d, ((CLONE_EPOCH + timedelta(hours=i)).timestamp(),) * 2)
        out[sha] = d
    return out


def gen(sha12: str, *, tiers: tuple[int, ...] = (1,), age_h: float | None = 0.0,
        clone: Path | None = Path("/scratch/base/x")) -> gc.Generation:
    """One generation, defaulting to a single tier-1 image plus a clone dir.
    `age_h` of None means no timestamp could be read for it."""
    return gc.Generation(
        sha12=sha12,
        images=tuple(f"pr-verify-base:{sha12}-t{t}" for t in tiers),
        clone_dir=clone,
        created=None if age_h is None else NOW - timedelta(hours=age_h))


class TestPlan:
    """The retention rule: the pinned SHA plus the most recently created other
    SHA survive; everything older is reclaimed."""

    def test_the_pinned_generation_survives_even_when_it_is_the_oldest(self):
        pinned, recent = gen("a" * 12, age_h=500.0), gen("b" * 12, age_h=1.0)
        p = gc.plan([pinned, recent], "a" * 12)
        assert "a" * 12 in p.keep
        assert p.images == () and p.clones == ()

    def test_the_most_recent_other_generation_survives(self):
        gens = [gen("a" * 12, age_h=0.0), gen("b" * 12, age_h=5.0),
                gen("c" * 12, age_h=50.0)]
        p = gc.plan(gens, "a" * 12)
        assert set(p.keep) == {"a" * 12, "b" * 12}

    def test_a_third_generation_is_reclaimed_with_its_image_and_clone(self):
        old = gen("c" * 12, age_h=50.0, clone=Path("/scratch/base/ccc"))
        p = gc.plan([gen("a" * 12, age_h=0.0), gen("b" * 12, age_h=5.0), old], "a" * 12)
        assert p.images == ("pr-verify-base:cccccccccccc-t1",)
        assert p.clones == (Path("/scratch/base/ccc"),)

    def test_every_tier_of_a_reclaimed_generation_is_listed(self):
        old = gen("c" * 12, tiers=(0, 1), age_h=50.0)
        p = gc.plan([gen("a" * 12), gen("b" * 12, age_h=5.0), old], "a" * 12)
        assert set(p.images) == {"pr-verify-base:cccccccccccc-t0",
                                 "pr-verify-base:cccccccccccc-t1"}

    def test_a_clone_with_no_image_is_reclaimed(self):
        orphan = gen("c" * 12, tiers=(), age_h=50.0, clone=Path("/scratch/base/ccc"))
        p = gc.plan([gen("a" * 12), gen("b" * 12, age_h=5.0), orphan], "a" * 12)
        assert p.images == ()
        assert p.clones == (Path("/scratch/base/ccc"),)

    def test_an_image_with_no_clone_is_reclaimed(self):
        orphan = gen("c" * 12, age_h=50.0, clone=None)
        p = gc.plan([gen("a" * 12), gen("b" * 12, age_h=5.0), orphan], "a" * 12)
        assert p.images == ("pr-verify-base:cccccccccccc-t1",)
        assert p.clones == ()

    def test_an_unreadable_timestamp_on_the_pin_never_reclaims_it(self):
        """A generation whose creation time could not be read sorts oldest. The
        pin is kept by identity, not by rank, so an unreadable stamp on the
        pinned SHA must not put it in the delete set."""
        pinned = gen("a" * 12, age_h=None)
        p = gc.plan([pinned, gen("b" * 12, age_h=1.0), gen("c" * 12, age_h=2.0)],
                    "a" * 12)
        assert "a" * 12 in p.keep
        assert not any("aaaa" in i for i in p.images)

    def test_unreadable_timestamps_sort_oldest_and_are_reclaimed_first(self):
        p = gc.plan([gen("a" * 12, age_h=0.0), gen("b" * 12, age_h=100.0),
                     gen("c" * 12, age_h=None)], "a" * 12)
        assert set(p.keep) == {"a" * 12, "b" * 12}

    def test_generations_tie_on_timestamp_break_deterministically(self):
        same = [gen("b" * 12, age_h=5.0), gen("c" * 12, age_h=5.0)]
        first = gc.plan([gen("a" * 12), *same], "a" * 12)
        second = gc.plan([gen("a" * 12), *reversed(same)], "a" * 12)
        assert first.keep == second.keep

    def test_with_no_pin_the_two_newest_generations_survive(self):
        """A machine that has never pinned still bounds its disk: the rule keeps
        KEEP_GENERATIONS, and with no SHA to protect they are simply the newest."""
        p = gc.plan([gen("a" * 12, age_h=0.0), gen("b" * 12, age_h=5.0),
                     gen("c" * 12, age_h=50.0)], None)
        assert set(p.keep) == {"a" * 12, "b" * 12}

    def test_a_pin_with_no_local_artifacts_still_protects_the_newest_other(self):
        """The pin was prepared on another host, so nothing local carries its
        SHA. It occupies a keep slot regardless, so only one local generation
        survives."""
        p = gc.plan([gen("b" * 12, age_h=5.0), gen("c" * 12, age_h=50.0)], "a" * 12)
        assert set(p.keep) == {"a" * 12, "b" * 12}
        assert p.images == ("pr-verify-base:cccccccccccc-t1",)

    def test_nothing_is_reclaimed_when_only_the_kept_generations_exist(self):
        p = gc.plan([gen("a" * 12), gen("b" * 12, age_h=5.0)], "a" * 12)
        assert p.images == () and p.clones == ()

    def test_an_empty_survey_plans_nothing(self):
        p = gc.plan([], "a" * 12)
        assert p.images == () and p.clones == () and p.keep == ("a" * 12,)


class TestListGenerations:
    """The survey: `docker image ls` output plus a scan of the scratch root,
    folded into one record per base SHA."""

    def _docker(self, stdout: str):
        def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout, "")
        return run

    def test_a_tag_and_its_clone_fold_into_one_generation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        clones = clone_dirs(tmp_path, "a")
        monkeypatch.setattr(gc.subprocess, "run", self._docker(
            f"{'a' * 12}-t1\t2026-08-10 09:00:00 +0000 UTC\n"))
        gens = gc.list_generations()
        assert len(gens) == 1
        assert gens[0].images == (f"pr-verify-base:{'a' * 12}-t1",)
        assert gens[0].clone_dir == clones["a"]
        assert gens[0].created == datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)

    def test_two_tiers_of_one_sha_are_one_generation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        monkeypatch.setattr(gc.subprocess, "run", self._docker(
            f"{'a' * 12}-t0\t2026-08-10 09:00:00 +0000 UTC\n"
            f"{'a' * 12}-t1\t2026-08-10 10:00:00 +0000 UTC\n"))
        gens = gc.list_generations()
        assert len(gens) == 1
        assert len(gens[0].images) == 2

    def test_a_generation_takes_its_newest_artifact_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        monkeypatch.setattr(gc.subprocess, "run", self._docker(
            f"{'a' * 12}-t0\t2026-08-10 09:00:00 +0000 UTC\n"
            f"{'a' * 12}-t1\t2026-08-10 11:00:00 +0000 UTC\n"))
        assert gc.list_generations()[0].created == datetime(
            2026, 8, 10, 11, 0, tzinfo=timezone.utc)

    def test_an_unparseable_created_stamp_reads_as_no_timestamp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        monkeypatch.setattr(gc.subprocess, "run", self._docker(
            f"{'a' * 12}-t1\tnot-a-date\n"))
        assert gc.list_generations()[0].created is None

    def test_a_tag_that_is_not_a_base_image_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        monkeypatch.setattr(gc.subprocess, "run", self._docker(
            "<none>\t2026-08-10 09:00:00 +0000 UTC\n"
            "latest\t2026-08-10 09:00:00 +0000 UTC\n"))
        assert gc.list_generations() == []

    def test_a_clone_dir_with_no_image_still_surveys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        (tmp_path / "base" / ("c" * 12)).mkdir(parents=True)
        monkeypatch.setattr(gc.subprocess, "run", self._docker(""))
        gens = gc.list_generations()
        assert len(gens) == 1
        assert gens[0].images == ()
        assert gens[0].created is not None

    def test_a_non_sha_directory_in_the_scratch_root_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        (tmp_path / "base" / "scratch-notes").mkdir(parents=True)
        monkeypatch.setattr(gc.subprocess, "run", self._docker(""))
        assert gc.list_generations() == []

    def test_a_missing_scratch_root_surveys_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path / "absent")
        monkeypatch.setattr(gc.subprocess, "run", self._docker(""))
        assert gc.list_generations() == []


class TestCollect:
    """collect runs inside prepare_base, so its contract is that it cannot fail
    a pin: every Docker or filesystem error is captured and reported, never
    raised."""

    def test_a_docker_failure_is_reported_not_raised(self, monkeypatch):
        def boom(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise OSError("docker: not found")
        monkeypatch.setattr(gc.subprocess, "run", boom)
        result = gc.collect("a" * 12)
        assert result["ok"] is False
        assert "docker" in result["error"]

    def test_a_dry_run_deletes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        doomed = clone_dirs(tmp_path, "c", "b", "a")["c"]
        calls: list[list[str]] = []

        def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(gc.subprocess, "run", run)
        result = gc.collect("a" * 12, dry_run=True)
        assert doomed.is_dir()
        assert not any("rmi" in c or "prune" in c for c in calls)
        assert result["reclaimed"] == ["c" * 12]

    def test_a_reclaimed_clone_is_removed_and_the_kept_ones_are_not(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        clone_dirs(tmp_path, "c", "b", "a")
        monkeypatch.setattr(gc.subprocess, "run", lambda cmd, **kw:
                            subprocess.CompletedProcess(cmd, 0, "", ""))
        gc.collect("a" * 12)
        assert (tmp_path / "base" / ("a" * 12)).is_dir()
        assert (tmp_path / "base" / ("b" * 12)).is_dir()
        assert not (tmp_path / "base" / ("c" * 12)).exists()

    def test_an_undeletable_clone_does_not_abort_the_rest_of_the_sweep(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        clone_dirs(tmp_path, "d", "c", "b", "a")
        pruned: list[list[str]] = []

        def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            if "prune" in cmd:
                pruned.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def rmtree(path: object, *a: object, **kw: object) -> None:
            raise OSError("device busy")

        monkeypatch.setattr(gc.subprocess, "run", run)
        monkeypatch.setattr(gc.shutil, "rmtree", rmtree)
        result = gc.collect("a" * 12)
        assert result["ok"] is True
        assert pruned, "the dangling/build-cache prune must still run"

    def test_the_build_side_prunes_are_scoped(self, tmp_path, monkeypatch):
        """The image prune is filtered to our own label and the builder prune to
        a 24h window, so neither reclaims another project's images or the cache
        of a build running right now."""
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        calls: list[list[str]] = []

        def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(gc.subprocess, "run", run)
        gc.collect("a" * 12)
        image_prune = [c for c in calls if c[1:3] == ["image", "prune"]]
        builder_prune = [c for c in calls if c[1:3] == ["builder", "prune"]]
        assert image_prune and f"label={gc.LABEL}" in image_prune[0]
        assert builder_prune and f"until={gc.BUILD_CACHE_KEEP_HOURS:g}h" in builder_prune[0]

    def test_images_are_removed_without_force(self, tmp_path, monkeypatch):
        """Every sandbox container runs --rm, so an un-forced rmi fails only
        while a phase container is live against that image — the last safety net
        under the retention rule."""
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        calls: list[list[str]] = []

        def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if cmd[1:3] == ["image", "ls"]:
                return subprocess.CompletedProcess(
                    cmd, 0, "".join(f"{s * 12}-t1\t2026-08-1{i} 09:00:00 +0000 UTC\n"
                                    for i, s in enumerate(("c", "b", "a"))), "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(gc.subprocess, "run", run)
        gc.collect("a" * 12)
        rmi = [c for c in calls if c[1] == "rmi"]
        assert rmi == [["docker", "rmi", f"pr-verify-base:{'c' * 12}-t1"]]
        assert not any("-f" in c or "--force" in c for c in rmi)

    def test_an_image_still_in_use_leaves_its_clone_in_place(self, tmp_path, monkeypatch):
        """A refused rmi means a phase container is running against that image.
        Its clone must survive too — the run reads both."""
        monkeypatch.setattr(gc, "SCRATCH", tmp_path)
        clone_dirs(tmp_path, "c", "b", "a")

        def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            if cmd[1:3] == ["image", "ls"]:
                return subprocess.CompletedProcess(
                    cmd, 0, "".join(f"{s * 12}-t1\t2026-08-1{i} 09:00:00 +0000 UTC\n"
                                    for i, s in enumerate(("c", "b", "a"))), "")
            if cmd[1] == "rmi":
                return subprocess.CompletedProcess(cmd, 1, "", "image is being used")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(gc.subprocess, "run", run)
        result = gc.collect("a" * 12)
        assert (tmp_path / "base" / ("c" * 12)).is_dir()
        assert result["reclaimed"] == []


class TestTeardown:
    """Unprovisioning removes every base generation, the sandbox image, and the
    scratch root — the pinned SHA included, since the pin goes with them."""

    def _stub(self, monkeypatch, tmp_path, *, rmi_ok=True):
        removed: list[str] = []
        pruned: list[int] = []
        scratch = tmp_path / "scratch"
        (scratch / "base" / ("a" * 12) / "src").mkdir(parents=True)
        (scratch / "base" / ("b" * 12)).mkdir(parents=True)
        (scratch / "other-file").write_text("x")
        monkeypatch.setattr(gc, "SCRATCH", scratch)
        monkeypatch.setattr(gc, "_base_images", lambda: {
            "a" * 12: [(f"pr-verify-base:{'a' * 12}-t1", NOW)],
            "b" * 12: [(f"pr-verify-base:{'b' * 12}-t0", NOW),
                        (f"pr-verify-base:{'b' * 12}-t1", NOW)]})
        monkeypatch.setattr(gc, "_rmi", lambda image: (removed.append(image), rmi_ok)[1])
        monkeypatch.setattr(gc, "_prune_build_leftovers", lambda: pruned.append(1))
        from pipeline import verify_driver
        monkeypatch.setattr(verify_driver, "sandbox_image", lambda: "pr-verify:pnpm-9.9.9")
        return removed, pruned, scratch

    def test_removes_every_generation_the_sandbox_image_and_the_scratch_root(
            self, monkeypatch, tmp_path):
        removed, pruned, scratch = self._stub(monkeypatch, tmp_path)
        r = gc.teardown()
        assert r["ok"] is True
        assert sorted(removed) == sorted([
            f"pr-verify-base:{'a' * 12}-t1", f"pr-verify-base:{'b' * 12}-t0",
            f"pr-verify-base:{'b' * 12}-t1", "pr-verify:pnpm-9.9.9"])
        assert sorted(r["images"]) == sorted(removed)
        assert not scratch.exists()
        assert r["scratch"] == str(scratch)
        assert pruned == [1]

    def test_dry_run_removes_nothing_and_reports_everything(self, monkeypatch, tmp_path):
        removed, pruned, scratch = self._stub(monkeypatch, tmp_path)
        r = gc.teardown(dry_run=True)
        assert removed == [] and pruned == []
        assert scratch.exists()
        assert len(r["images"]) == 4 and r["scratch"] == str(scratch)

    def test_an_image_that_will_not_go_is_reported_not_hidden(self, monkeypatch, tmp_path):
        removed, _, scratch = self._stub(monkeypatch, tmp_path, rmi_ok=False)
        r = gc.teardown()
        assert r["ok"] is False
        assert r["images"] == []
        assert len(r["kept_images"]) == 4
        assert not scratch.exists()
