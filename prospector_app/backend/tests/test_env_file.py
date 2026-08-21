"""The one atomic .env merge-and-replace. This file holds the store password and
both credential paths, so a write that mangles an unrelated line costs more than
a failed write."""
from __future__ import annotations

import pytest

from prospector_app.backend import env_file

ENV = """\
TRIAGE_REPO=owner/name
TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres
# TRIAGE_VERIFY_WORKER=1
"""


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(ENV)
    monkeypatch.setattr(env_file, "ENV_PATH", p)
    return p


class TestMerge:
    def test_replaces_a_commented_key_in_place(self):
        out = env_file.merge(ENV, {"TRIAGE_VERIFY_WORKER": "1"})
        assert "TRIAGE_VERIFY_WORKER=1\n" in out
        assert "# TRIAGE_VERIFY_WORKER=1" not in out

    def test_appends_a_key_the_file_never_mentioned(self):
        out = env_file.merge(ENV, {"TRIAGE_BOT_APP_ID": "12345"})
        assert "TRIAGE_BOT_APP_ID=12345\n" in out

    def test_keeps_every_other_line_byte_for_byte(self):
        out = env_file.merge(ENV, {"TRIAGE_VERIFY_WORKER": "1"})
        assert "TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres" in out
        assert "TRIAGE_REPO=owner/name" in out


class TestWrite:
    def test_round_trips(self, path):
        env_file.write({"TRIAGE_BOT_APP_ID": "12345"})
        assert "TRIAGE_BOT_APP_ID=12345" in path.read_text()

    def test_is_owner_only(self, path):
        env_file.write({"TRIAGE_BOT_APP_ID": "12345"})
        assert path.stat().st_mode & 0o777 == 0o600

    def test_creates_the_file_when_absent(self, tmp_path, monkeypatch):
        p = tmp_path / "fresh" / ".env"
        p.parent.mkdir()
        monkeypatch.setattr(env_file, "ENV_PATH", p)
        env_file.write({"TRIAGE_REPO": "acme/widgets"})
        assert "TRIAGE_REPO=acme/widgets" in p.read_text()
