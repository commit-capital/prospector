"""Provisioning this machine as a worker: what the readiness report sees, and
what the flag writer is allowed to touch.

The allowlist is the load-bearing part. `.env` holds the store password and the
paths to both credentials, so these tests pin that a flag write leaves every
other line exactly as it found it."""
from __future__ import annotations

import pytest

from prospector_app.backend import worker_control, worker_readiness

ENV = """\
TRIAGE_REPO=owner/name
TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres
TRIAGE_BOT_KEY_FILE=~/.config/app/private-key.pem
# TRIAGE_VERIFY_WORKER=1
TRIAGE_FIX_WORKER=1
"""


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(ENV)
    monkeypatch.setattr(worker_control, "ENV_PATH", path)
    return path


class TestSetFlags:
    def test_sets_a_commented_flag_in_place(self, env_file, monkeypatch):
        monkeypatch.delenv("TRIAGE_VERIFY_WORKER", raising=False)
        worker_control.set_flags({"TRIAGE_VERIFY_WORKER": "1"})
        assert "TRIAGE_VERIFY_WORKER=1\n" in env_file.read_text()
        assert "# TRIAGE_VERIFY_WORKER=1" not in env_file.read_text()

    def test_leaves_every_other_line_byte_for_byte(self, env_file):
        worker_control.set_flags({"TRIAGE_FIX_WORKER": ""})
        text = env_file.read_text()
        assert "TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres" in text
        assert "TRIAGE_BOT_KEY_FILE=~/.config/app/private-key.pem" in text
        assert "TRIAGE_REPO=owner/name" in text

    def test_appends_a_flag_the_file_never_mentioned(self, env_file):
        worker_control.set_flags({"TRIAGE_FIX_AUTOHUNT": "1"})
        assert "TRIAGE_FIX_AUTOHUNT=1" in env_file.read_text()

    def test_a_key_outside_the_allowlist_is_refused(self, env_file):
        with pytest.raises(ValueError, match="not a worker flag"):
            worker_control.set_flags({"TRIAGE_STORE_URL": "postgresql://mine"})
        assert "sup3rsecret" in env_file.read_text()

    def test_an_unknown_autofix_action_is_refused(self, env_file):
        with pytest.raises(ValueError, match="not an autofix action"):
            worker_control.set_flags({"TRIAGE_FIX_AUTOPUSH": "update,teleport"})

    def test_a_non_boolean_switch_is_refused(self, env_file):
        with pytest.raises(ValueError, match="TRIAGE_VERIFY_WORKER"):
            worker_control.set_flags({"TRIAGE_VERIFY_WORKER": "yes"})

    def test_a_refused_write_leaves_the_file_untouched(self, env_file):
        before = env_file.read_text()
        with pytest.raises(ValueError):
            worker_control.set_flags({"TRIAGE_FIX_AUTOPUSH": "nope"})
        assert env_file.read_text() == before

    def test_the_written_file_is_not_group_or_world_readable(self, env_file):
        worker_control.set_flags({"TRIAGE_FIX_AUTOHUNT": "1"})
        assert env_file.stat().st_mode & 0o077 == 0


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
    def test_a_check_that_raises_reads_as_failing(self, monkeypatch):
        """A check that cannot answer is not evidence the machine is ready."""
        def boom():
            raise RuntimeError("docker exploded")
        monkeypatch.setattr(worker_readiness, "_docker_daemon", boom)
        monkeypatch.setattr(
            worker_readiness, "_CHECKS",
            [("docker", "Docker daemon", boom, True)])
        rows = worker_readiness.checks()
        assert rows[0]["ok"] is False
        assert "docker exploded" in rows[0]["detail"]

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
