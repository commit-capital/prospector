"""What the wizard is allowed to write, and what it refuses.

The step allowlist is load-bearing: step 1 names the repository and the store,
so it closes the moment a deployment is configured. A configured Prospector
cannot be retargeted at another repository or another database over HTTP.
"""
from __future__ import annotations

import json
import os

import pytest

from prospector_app.backend import env_file, onboarding

ENV = """\
TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres
"""

PROFILE: dict[str, object] = {
    "version": 1, "subsystems": [{"name": "core", "match_terms": ["core"]}]}


@pytest.fixture
def files(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(ENV)
    prof = tmp_path / "profile.json"
    monkeypatch.setattr(env_file, "ENV_PATH", env)
    monkeypatch.setattr(onboarding, "PROFILE_PATH", prof)
    monkeypatch.setattr(onboarding, "reconfigure", lambda applied: None)
    return env, prof


@pytest.fixture
def adopting(tmp_path, monkeypatch):
    """Like `files`, but the process really adopts what it writes — which is
    what a route test is for. Puts the suite's own configuration back after,
    since adoption updates os.environ and repoints the store."""
    from prospector_app.backend import data
    env = tmp_path / ".env"
    env.write_text(ENV)
    prof = tmp_path / "profile.json"
    monkeypatch.setattr(env_file, "ENV_PATH", env)
    monkeypatch.setattr(onboarding, "PROFILE_PATH", prof)
    before = dict(os.environ)
    yield env, prof
    os.environ.clear()
    os.environ.update(before)
    data.reset()


class TestStepAllowlist:
    def test_step_one_writes_the_deployment_target(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        env, _ = files
        onboarding.apply("connect", {"TRIAGE_REPO": "acme/widgets"}, None)
        assert "TRIAGE_REPO=acme/widgets" in env.read_text()

    def test_step_one_is_refused_once_configured(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        with pytest.raises(ValueError, match="already configured"):
            onboarding.apply("connect", {"TRIAGE_REPO": "attacker/repo"}, None)

    def test_the_store_url_is_refused_once_configured(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        with pytest.raises(ValueError, match="already configured"):
            onboarding.apply("connect", {"TRIAGE_STORE_URL": "sqlite:///evil.db"}, None)

    def test_the_bot_identity_is_writable_while_configured(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        env, _ = files
        onboarding.apply("writes", {"TRIAGE_BOT_LOGIN": "acme-bot"}, None)
        assert "TRIAGE_BOT_LOGIN=acme-bot" in env.read_text()

    def test_a_key_outside_the_step_is_a_hard_error(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        with pytest.raises(ValueError, match="not writable"):
            onboarding.apply("connect", {"TRIAGE_FIX_AUTOPUSH": "fix"}, None)

    def test_a_lane_switch_is_not_reachable_from_here(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        with pytest.raises(ValueError, match="not writable"):
            onboarding.apply("worker", {"TRIAGE_FIX_WORKER": "1"}, None)

    def test_an_unknown_step_is_a_hard_error(self, files):
        with pytest.raises(ValueError, match="not a step"):
            onboarding.apply("whatever", {}, None)


class TestValidation:
    def test_a_malformed_repo_is_refused(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        with pytest.raises(ValueError, match="owner/name"):
            onboarding.apply("connect", {"TRIAGE_REPO": "widgets"}, None)

    def test_a_profile_the_parser_rejects_is_never_written(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        env, prof = files
        with pytest.raises(ValueError, match="subsystems"):
            onboarding.apply("connect", {"TRIAGE_REPO": "acme/widgets"},
                             {"version": 1, "subsystems": "not-a-list"})
        assert not prof.exists()
        assert "TRIAGE_REPO" not in env.read_text()

    def test_unrelated_env_lines_survive(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        env, _ = files
        onboarding.apply("connect", {"TRIAGE_REPO": "acme/widgets"}, PROFILE)
        assert "sup3rsecret" in env.read_text()

    def test_the_previous_profile_is_restored_when_the_env_write_fails(
            self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        _, prof = files
        prof.write_text(json.dumps({"version": 1, "subsystems": []}))

        def boom(updates):
            raise OSError("disk full")

        monkeypatch.setattr(env_file, "write", boom)
        with pytest.raises(OSError):
            onboarding.apply("connect", {"TRIAGE_REPO": "acme/widgets"}, PROFILE)
        assert json.loads(prof.read_text())["subsystems"] == []


class TestBundle:
    def test_round_trips(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setenv("TRIAGE_STORE_URL", "sqlite:///team.db")
        _, prof = files
        prof.write_text(json.dumps(PROFILE))
        monkeypatch.setenv("TRIAGE_PROFILE", str(prof))
        text = json.dumps(onboarding.build_bundle())
        env, doc = onboarding.parse_bundle(text)
        assert env["TRIAGE_REPO"] == "acme/widgets"
        assert env["TRIAGE_STORE_URL"] == "sqlite:///team.db"
        assert doc == PROFILE

    def test_an_unknown_version_is_refused_with_what_it_saw(self):
        with pytest.raises(ValueError, match="version 99"):
            onboarding.parse_bundle(json.dumps({"version": 99, "env": {}}))

    def test_junk_is_refused_without_a_traceback(self):
        with pytest.raises(ValueError, match="not a Prospector bundle"):
            onboarding.parse_bundle("hello")

    def test_a_lane_switch_never_travels_in_a_bundle(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setenv("TRIAGE_FIX_WORKER", "1")
        assert "TRIAGE_FIX_WORKER" not in onboarding.build_bundle()["env"]

    def test_a_credential_path_never_travels_in_a_bundle(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setenv("TRIAGE_BOT_KEY_FILE", "/home/me/secret.pem")
        assert "TRIAGE_BOT_KEY_FILE" not in onboarding.build_bundle()["env"]


class TestProbe:
    def test_reports_a_reachable_store_with_its_counts(self, tmp_path, monkeypatch):
        from pipeline.store import Store
        monkeypatch.setenv("TRIAGE_STORE_URL", f"sqlite:///{tmp_path}/probe.db")
        Store()  # create_all, so the tables the probe counts exist
        found = onboarding.probe(store_url=f"sqlite:///{tmp_path}/probe.db",
                                 repo=None, key_file=None)
        assert found["store"]["ok"] is True
        assert found["store"]["prs"] == 0

    def test_an_unreachable_store_is_a_finding_not_an_exception(self):
        found = onboarding.probe(
            store_url="postgresql+psycopg://nope:nope@127.0.0.1:1/none",
            repo=None, key_file=None)
        assert found["store"]["ok"] is False
        assert isinstance(found["store"]["problem"], str)

    def test_never_echoes_the_password_back(self):
        url = "postgresql+psycopg://user:sup3rsecret@127.0.0.1:1/none"
        found = onboarding.probe(store_url=url, repo=None, key_file=None)
        assert "sup3rsecret" not in json.dumps(found)

    def test_a_malformed_repo_is_a_finding(self):
        found = onboarding.probe(store_url=None, repo="widgets", key_file=None)
        assert found["repo"]["ok"] is False

    def test_a_missing_key_file_is_a_finding(self, tmp_path):
        found = onboarding.probe(store_url=None, repo=None,
                                 key_file=str(tmp_path / "absent.pem"))
        assert found["key_file"]["ok"] is False

    def test_writes_nothing(self, files):
        env, prof = files
        before = env.read_text()
        onboarding.probe(store_url="sqlite:///probe.db", repo=None, key_file=None)
        assert env.read_text() == before
        assert not prof.exists()


class TestBundleContents:
    """Deployment facts travel; this machine's own layout does not."""

    @pytest.fixture(autouse=True)
    def deployment(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "owner/name")
        monkeypatch.setenv("TRIAGE_BOT_LOGIN", "the-bot")
        monkeypatch.setenv("TRIAGE_STORE_URL",
                           "postgresql+psycopg://user:sup3rsecret@host:6543/postgres")
        monkeypatch.setenv("TRIAGE_BOT_KEY_FILE", "/Users/me/.config/app/key.pem")
        monkeypatch.setenv("TRIAGE_PUSH_SSH_KEY_FILE", "/Users/me/.ssh/pushkey")

    def test_prefills_the_deployment_facts(self):
        env = onboarding.build_bundle()["env"]
        assert env["TRIAGE_REPO"] == "owner/name"
        assert env["TRIAGE_BOT_LOGIN"] == "the-bot"

    def test_carries_the_store_url_so_one_paste_is_enough(self):
        env = onboarding.build_bundle()["env"]
        assert env["TRIAGE_STORE_URL"] == (
            "postgresql+psycopg://user:sup3rsecret@host:6543/postgres")

    def test_local_credential_paths_never_travel(self):
        """Another machine's paths would be wrong anyway; shipping them only
        leaks how this machine is laid out."""
        text = json.dumps(onboarding.build_bundle())
        assert "/Users/me/.config/app/key.pem" not in text
        assert "/Users/me/.ssh/pushkey" not in text


class TestRoutes:
    def test_apply_from_a_bundle_configures_an_unconfigured_app(
            self, adopting, monkeypatch):
        from fastapi.testclient import TestClient
        from prospector_app.backend import app as app_mod
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        bundle = json.dumps({"version": 1,
                             "env": {"TRIAGE_REPO": "acme/widgets"},
                             "profile": PROFILE})
        r = client.post("/api/onboarding/apply",
                        json={"step": "connect", "bundle": bundle})
        assert r.status_code == 200
        assert r.json()["configured"] is True

    def test_a_bad_bundle_is_a_400_not_a_500(self, files, monkeypatch):
        from fastapi.testclient import TestClient
        from prospector_app.backend import app as app_mod
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        r = client.post("/api/onboarding/apply",
                        json={"step": "connect", "bundle": "hello"})
        assert r.status_code == 400
        assert "not a Prospector bundle" in r.json()["detail"]

    def test_retargeting_a_configured_app_is_refused(self, files, monkeypatch):
        from fastapi.testclient import TestClient
        from prospector_app.backend import app as app_mod
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        r = client.post("/api/onboarding/apply",
                        json={"step": "connect",
                              "env": {"TRIAGE_REPO": "attacker/repo"}})
        assert r.status_code == 400
        assert "already configured" in r.json()["detail"]
