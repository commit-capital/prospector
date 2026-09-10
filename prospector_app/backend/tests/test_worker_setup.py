"""Provisioning this machine as a worker: what the readiness report sees, and
what the flag writer is allowed to touch.

The allowlist is the load-bearing part. `.env` holds the store password and the
paths to both credentials, so these tests pin that a flag write leaves every
other line exactly as it found it."""
from __future__ import annotations

import pytest

from prospector_app.backend import env_file, worker_control, worker_readiness

ENV = """\
TRIAGE_REPO=owner/name
TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres
TRIAGE_BOT_KEY_FILE=~/.config/app/private-key.pem
# TRIAGE_VERIFY_WORKER=1
TRIAGE_FIX_WORKER=1
"""


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(ENV)
    monkeypatch.setattr(env_file, "ENV_PATH", path)
    return path


class TestSetFlags:
    def test_sets_a_commented_flag_in_place(self, env_path, monkeypatch):
        monkeypatch.delenv("TRIAGE_VERIFY_WORKER", raising=False)
        worker_control.set_flags({"TRIAGE_VERIFY_WORKER": "1"})
        assert "TRIAGE_VERIFY_WORKER=1\n" in env_path.read_text()
        assert "# TRIAGE_VERIFY_WORKER=1" not in env_path.read_text()

    def test_leaves_every_other_line_byte_for_byte(self, env_path):
        worker_control.set_flags({"TRIAGE_FIX_WORKER": ""})
        text = env_path.read_text()
        assert "TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres" in text
        assert "TRIAGE_BOT_KEY_FILE=~/.config/app/private-key.pem" in text
        assert "TRIAGE_REPO=owner/name" in text

    def test_appends_a_flag_the_file_never_mentioned(self, env_path):
        worker_control.set_flags({"TRIAGE_FIX_AUTOHUNT": "1"})
        assert "TRIAGE_FIX_AUTOHUNT=1" in env_path.read_text()

    def test_a_key_outside_the_allowlist_is_refused(self, env_path):
        with pytest.raises(ValueError, match="not a worker flag"):
            worker_control.set_flags({"TRIAGE_STORE_URL": "postgresql://mine"})
        assert "sup3rsecret" in env_path.read_text()

    def test_an_unknown_autofix_action_is_refused(self, env_path):
        with pytest.raises(ValueError, match="not an autofix action"):
            worker_control.set_flags({"TRIAGE_FIX_AUTOPUSH": "update,teleport"})

    def test_a_non_boolean_switch_is_refused(self, env_path):
        with pytest.raises(ValueError, match="TRIAGE_VERIFY_WORKER"):
            worker_control.set_flags({"TRIAGE_VERIFY_WORKER": "yes"})

    @pytest.mark.parametrize("key", ["TRIAGE_FIX_HUNT_FIX", "TRIAGE_FIX_HUNT_RESOLVE"])
    def test_the_hunt_opt_ins_are_lane_switches(self, env_path, key):
        worker_control.set_flags({key: "1"})
        assert f"{key}=1" in env_path.read_text()
        with pytest.raises(ValueError, match=key):
            worker_control.set_flags({key: "yes"})

    def test_a_refused_write_leaves_the_file_untouched(self, env_path):
        before = env_path.read_text()
        with pytest.raises(ValueError):
            worker_control.set_flags({"TRIAGE_FIX_AUTOPUSH": "nope"})
        assert env_path.read_text() == before

    def test_the_written_file_is_not_group_or_world_readable(self, env_path):
        worker_control.set_flags({"TRIAGE_FIX_AUTOHUNT": "1"})
        assert env_path.stat().st_mode & 0o077 == 0


class TestApply:
    def test_reports_each_lane_separately(self, monkeypatch):
        """One lane refusing must never hide the other succeeding."""
        monkeypatch.setattr(worker_control.verify_worker, "enabled", lambda: True)
        monkeypatch.setattr(worker_control.verify_worker, "startup", lambda: True)
        monkeypatch.setattr(worker_control.fix_worker, "enabled", lambda: True)
        monkeypatch.setattr(worker_control.fix_worker, "startup", lambda: False)
        assert worker_control.apply()["lanes"] == {"verify": "running", "fix": "refused"}

    def test_a_lane_turned_off_is_stopped(self, monkeypatch):
        monkeypatch.setattr(worker_control.verify_worker, "enabled", lambda: False)
        monkeypatch.setattr(worker_control.verify_worker, "running", lambda: True)
        monkeypatch.setattr(worker_control.verify_worker, "shutdown", lambda: True)
        monkeypatch.setattr(worker_control.fix_worker, "enabled", lambda: False)
        monkeypatch.setattr(worker_control.fix_worker, "running", lambda: False)
        assert worker_control.apply()["lanes"] == {"verify": "stopped", "fix": "off"}

    def test_a_stop_still_finishing_reports_stopping(self, monkeypatch):
        """The loops are signalled; the run in flight finishes. Not a failure."""
        monkeypatch.setattr(worker_control.verify_worker, "enabled", lambda: False)
        monkeypatch.setattr(worker_control.verify_worker, "running", lambda: True)
        monkeypatch.setattr(worker_control.verify_worker, "shutdown", lambda: False)
        monkeypatch.setattr(worker_control.fix_worker, "enabled", lambda: False)
        monkeypatch.setattr(worker_control.fix_worker, "running", lambda: False)
        assert worker_control.apply()["lanes"]["verify"] == "stopping"


class TestReadiness:
    @pytest.mark.parametrize(
        ("system", "install", "start"),
        [
            ("Darwin", "install Docker and Colima", "start it with `colima start`"),
            ("Linux", "install Docker Engine", "start it with `sudo systemctl start docker`"),
        ],
    )
    def test_docker_remedies_match_the_host(self, monkeypatch, system, install, start):
        monkeypatch.setattr(worker_readiness.platform, "system", lambda: system)
        monkeypatch.setattr(worker_readiness.shutil, "which", lambda _: None)
        assert worker_readiness._docker_daemon() == (
            False, "Docker is not installed", install)

        monkeypatch.setattr(worker_readiness.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(worker_readiness.verify_driver, "daemon_available", lambda: False)
        assert worker_readiness._docker_daemon() == (
            False, "Docker is installed but not running", start)

    def test_a_check_failure_is_logged_without_reaching_the_readiness_response(
            self, monkeypatch, caplog):
        """A check that cannot answer is not evidence the machine is ready."""
        def boom():
            raise RuntimeError("docker exploded")
        monkeypatch.setattr(worker_readiness, "_docker_daemon", boom)
        monkeypatch.setattr(
            worker_readiness, "_CHECKS",
            [("docker", "Docker daemon", boom, True)])
        with caplog.at_level("ERROR", logger="prospector_app.backend.worker_readiness"):
            rows = worker_readiness.checks()
        assert rows[0]["ok"] is False
        assert rows[0]["detail"] == "check failed unexpectedly"
        assert rows[0]["remedy"] == "see server logs"
        assert "docker exploded" not in repr(rows[0])
        assert "docker exploded" in caplog.text

    def test_ready_ignores_the_non_blocking_rows(self, monkeypatch):
        """A machine may deliberately run verification and not autofix."""
        monkeypatch.setattr(
            worker_readiness, "_CHECKS",
            [("docker", "Docker", lambda: (True, "up", ""), True),
             ("push_identity", "Push", lambda: (False, "none", "set it"), False),
             ("fix_flag", "Fix", lambda: (False, "off", "turn on"), False)])
        report = worker_readiness.report()
        assert report["ready"] is True
        assert report["autofix_ready"] is False



    def test_fix_policy_row_reads_the_profiles_fixable_gates(self, monkeypatch):
        """The row the hunt-fix switch waits on: an unguided fix needs the
        profile's opt-in, so a machine whose profile names none is told where
        the gates come from."""
        from pipeline import profile
        opted = profile.parse_profile(
            {"version": 1, "autofix": {"fixable_gates": ["ci", "review"]}}, "test")
        monkeypatch.setattr(profile, "active", lambda: opted)
        ok, detail, remedy = worker_readiness._fix_policy()
        assert ok and "ci, review" in detail

        monkeypatch.setattr(profile, "active", lambda: profile.parse_profile({"version": 1}, "test"))
        ok, detail, remedy = worker_readiness._fix_policy()
        assert not ok
        assert "fixable_gates" in remedy
        assert "card below" in remedy

    def test_fix_policy_row_never_blocks_readiness(self):
        row = next(c for c in worker_readiness.checks() if c["key"] == "fix_policy")
        assert row["blocking"] is False

    def test_sandbox_row_names_the_images_built_under_other_tags(self, monkeypatch):
        """The image the profile's pin names is missing while an image built
        under another tag sits in the daemon: the row says which, so the fix
        reads as a rebuild under the current tag rather than a first build."""
        vd = worker_readiness.verify_driver
        monkeypatch.setattr(vd, "sandbox_image", lambda: "pr-verify:pnpm-9.15.4")
        monkeypatch.setattr(vd, "daemon_available", lambda: True)
        monkeypatch.setattr(vd, "image_exists", lambda tag: False)
        monkeypatch.setattr(vd, "sandbox_images", lambda: ["pr-verify:local"])
        ok, detail, remedy = worker_readiness._sandbox_image()
        assert ok is False
        assert "pr-verify:pnpm-9.15.4 is not built" in detail
        assert "pr-verify:local" in detail
        assert remedy == "run build-image here"

    def test_sandbox_row_with_no_image_at_all_reads_as_a_first_build(self, monkeypatch):
        vd = worker_readiness.verify_driver
        monkeypatch.setattr(vd, "sandbox_image", lambda: "pr-verify:pnpm-9.15.4")
        monkeypatch.setattr(vd, "daemon_available", lambda: True)
        monkeypatch.setattr(vd, "image_exists", lambda tag: False)
        monkeypatch.setattr(vd, "sandbox_images", lambda: [])
        ok, detail, remedy = worker_readiness._sandbox_image()
        assert (ok, detail, remedy) == (
            False, "pr-verify:pnpm-9.15.4 is not built", "run build-image here")
