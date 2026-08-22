"""The contributor-push credential: generated owner-only, accepted only when
GitHub names the account the operator asked for."""
from __future__ import annotations

import shutil
import stat
import subprocess

import pytest

from prospector_app.backend import onboarding, push_identity

GREETING = ("Hi octocat! You've successfully authenticated, but GitHub does not "
            "provide shell access.\n")


def test_noreply_email_is_the_per_account_form():
    assert push_identity.noreply_email(583231, "octocat") == \
        "583231+octocat@users.noreply.github.com"


class TestReadProbe:
    def test_the_named_account_passes(self):
        p = push_identity.read_probe(1, GREETING, "octocat")
        assert p["ok"] is True and p["login"] == "octocat"

    def test_login_comparison_ignores_case(self):
        assert push_identity.read_probe(1, GREETING, "OctoCat")["ok"] is True

    def test_another_account_is_named_in_the_refusal(self):
        p = push_identity.read_probe(1, GREETING, "my-bot")
        assert p["ok"] is False
        assert p["login"] == "octocat"
        assert "attached to octocat, not my-bot" in (p["problem"] or "")

    def test_a_rejected_key_says_to_add_the_public_half(self):
        p = push_identity.read_probe(255, "git@github.com: Permission denied (publickey).\n", "my-bot")
        assert p["ok"] is False
        assert "my-bot" in (p["problem"] or "")

    def test_no_answer_is_a_finding_not_a_pass(self):
        p = push_identity.read_probe(255, "ssh: connect to host github.com port 22: timed out\n", "x")
        assert p["ok"] is False and "GitHub" in (p["problem"] or "")


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
class TestGenerateKey:
    @pytest.fixture(autouse=True)
    def isolated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(onboarding, "KEY_DIR", tmp_path / "keys")

    def test_files_an_owner_only_ed25519_key(self, tmp_path):
        info = push_identity.generate_key("my-bot")
        path = tmp_path / "keys" / "my-bot" / "push-key"
        assert info["path"] == str(path)
        assert info["public_key"].startswith("ssh-ed25519 ")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert "PRIVATE KEY" in path.read_text()

    def test_is_idempotent(self):
        first = push_identity.generate_key("my-bot")
        assert push_identity.generate_key("my-bot") == first

    def test_a_login_no_key_can_be_filed_under_is_refused(self):
        with pytest.raises(ValueError, match="login"):
            push_identity.generate_key("../etc")

    def test_public_key_of_a_missing_file_is_a_value_error(self, tmp_path):
        with pytest.raises(ValueError):
            push_identity.public_key(tmp_path / "absent")

    def test_probe_of_a_missing_key_never_runs_ssh(self, tmp_path):
        p = push_identity.probe_key(tmp_path / "absent", "x")
        assert p["ok"] is False and "no key" in (p["problem"] or "")


def test_probe_offers_only_the_pinned_key(tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("x")
    seen: list[list[str]] = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        if cmd[0] == "ssh-keygen":
            return subprocess.CompletedProcess(cmd, 0, "ssh-ed25519 AAAA", "")
        return subprocess.CompletedProcess(cmd, 1, "", GREETING)

    monkeypatch.setattr(push_identity.subprocess, "run", fake_run)
    assert push_identity.probe_key(key, "octocat")["ok"] is True
    cmd = [c for c in seen if c[0] == "ssh"][0]
    assert cmd[:2] == ["ssh", "-T"]
    assert cmd[cmd.index("-F") + 1] == "/dev/null"
    assert "IdentitiesOnly=yes" in cmd and "IdentityAgent=none" in cmd
    assert "BatchMode=yes" in cmd


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_probe_names_a_key_it_cannot_read_unattended(tmp_path, monkeypatch):
    key = tmp_path / "locked"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "hunter2", "-f", str(key)],
                   check=True, stdin=subprocess.DEVNULL)
    real_run = subprocess.run

    def guarded(cmd, **kw):
        assert cmd[0] != "ssh", "ssh must not be reached for a key ssh-keygen cannot read"
        return real_run(cmd, **kw)

    monkeypatch.setattr(push_identity.subprocess, "run", guarded)
    p = push_identity.probe_key(key, "octocat")
    assert p["ok"] is False and "passphrase" in (p["problem"] or "")
