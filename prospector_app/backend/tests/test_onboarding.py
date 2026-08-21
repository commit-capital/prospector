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


PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----\n"


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    """A readable bot key on this machine, and an isolated place for a joiner
    to file one under."""
    pem = tmp_path / "sharer.pem"
    pem.write_text(PEM)
    monkeypatch.setenv("TRIAGE_BOT_KEY_FILE", str(pem))
    monkeypatch.setattr(onboarding, "KEY_DIR", tmp_path / "keys")
    return pem


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


class TestJoin:
    def test_a_real_bundle_lands_whole(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setenv("TRIAGE_STORE_URL", "sqlite:///team.db")
        monkeypatch.setenv("TRIAGE_BOT_LOGIN", "acme-bot")
        monkeypatch.setenv("TRIAGE_BOT_APP_ID", "12345")
        b = onboarding.parse_bundle(json.dumps(onboarding.build_bundle()))
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        env, _ = files
        onboarding.apply("join", b.env, b.profile)
        text = env.read_text()
        assert "TRIAGE_REPO=acme/widgets" in text
        assert "TRIAGE_BOT_LOGIN=acme-bot" in text
        assert "TRIAGE_BOT_APP_ID=12345" in text
        assert "TRIAGE_BOT_KEY_FILE" not in text

    def test_join_is_refused_once_configured(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        with pytest.raises(ValueError, match="already configured"):
            onboarding.apply("join", {"TRIAGE_REPO": "attacker/repo"}, None)


class TestJoinWithKey:
    """A bundle the sharer chose to put the bot's key in makes the joiner's
    machine a keyed one: the key is filed outside the repo, owner-only, and
    named in .env."""

    JOIN = {"TRIAGE_REPO": "acme/widgets", "TRIAGE_BOT_LOGIN": "acme-bot",
            "TRIAGE_BOT_APP_ID": "12345"}

    def test_the_key_is_filed_and_named(self, files, keyed, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        env, _ = files
        onboarding.apply("join", dict(self.JOIN), None, bot_key_pem=PEM)
        installed = onboarding.KEY_DIR / "acme-bot" / "private-key.pem"
        assert installed.read_text() == PEM
        assert installed.stat().st_mode & 0o777 == 0o600
        assert installed.parent.stat().st_mode & 0o777 == 0o700
        assert f"TRIAGE_BOT_KEY_FILE={installed}" in env.read_text()

    def test_the_key_lives_outside_the_checkout(self):
        assert onboarding.KEY_DIR.resolve() != onboarding.settings.REPO_ROOT
        assert onboarding.settings.REPO_ROOT not in onboarding.KEY_DIR.resolve().parents

    def test_the_key_needs_a_login_to_be_filed_under(self, files, keyed, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        with pytest.raises(ValueError, match="TRIAGE_BOT_LOGIN"):
            onboarding.apply("join", {"TRIAGE_REPO": "acme/widgets"}, None,
                             bot_key_pem=PEM)

    def test_text_that_is_not_a_private_key_is_refused(self, files, keyed, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        with pytest.raises(ValueError, match="private key"):
            onboarding.apply("join", dict(self.JOIN), None, bot_key_pem="hello")
        assert not (onboarding.KEY_DIR / "acme-bot").exists()

    def test_a_key_travels_only_in_a_bundle(self, files, keyed, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        with pytest.raises(ValueError, match="bundle"):
            onboarding.apply("writes", {"TRIAGE_BOT_LOGIN": "acme-bot"}, None,
                             bot_key_pem=PEM)

    def test_the_key_path_is_never_client_writable_in_join(self):
        assert "TRIAGE_BOT_KEY_FILE" not in onboarding.STEP_KEYS["join"]

    def test_the_filed_key_is_removed_when_the_env_write_fails(
            self, files, keyed, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)

        def boom(updates):
            raise OSError("disk full")
        monkeypatch.setattr(env_file, "write", boom)
        with pytest.raises(OSError):
            onboarding.apply("join", dict(self.JOIN), None, bot_key_pem=PEM)
        assert not (onboarding.KEY_DIR / "acme-bot" / "private-key.pem").exists()


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
        b = onboarding.parse_bundle(text)
        assert b.env["TRIAGE_REPO"] == "acme/widgets"
        assert b.env["TRIAGE_STORE_URL"] == "sqlite:///team.db"
        assert b.profile == PROFILE
        assert b.bot_key_pem is None

    def test_the_key_travels_only_on_request(self, keyed, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setenv("TRIAGE_BOT_LOGIN", "acme-bot")
        assert "bot_key_pem" not in onboarding.build_bundle()
        with_key = onboarding.build_bundle(include_key=True)
        assert with_key["bot_key_pem"] == PEM
        assert onboarding.parse_bundle(json.dumps(with_key)).bot_key_pem == PEM

    def test_including_a_key_this_machine_lacks_is_refused(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setenv("TRIAGE_BOT_LOGIN", "acme-bot")
        monkeypatch.setenv("TRIAGE_BOT_KEY_FILE", "/nonexistent/key.pem")
        with pytest.raises(ValueError, match="key"):
            onboarding.build_bundle(include_key=True)

    def test_including_a_key_without_a_login_is_refused(self, keyed, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.delenv("TRIAGE_BOT_LOGIN", raising=False)
        with pytest.raises(ValueError, match="TRIAGE_BOT_LOGIN"):
            onboarding.build_bundle(include_key=True)

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


class TestAgentStep:
    def test_writes_the_provider_choice_while_configured(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        env, _ = files
        onboarding.apply("agent", {"TRIAGE_AGENT_PROVIDER": "claude"}, None)
        assert "TRIAGE_AGENT_PROVIDER=claude" in env.read_text()

    def test_none_turns_agent_support_off(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        env, _ = files
        onboarding.apply("agent", {"TRIAGE_AGENT_PROVIDER": "none"}, None)
        assert "TRIAGE_AGENT_PROVIDER=none" in env.read_text()

    def test_an_unsupported_provider_is_refused(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        with pytest.raises(ValueError, match="claude.*none"):
            onboarding.apply("agent", {"TRIAGE_AGENT_PROVIDER": "codex"}, None)

    def test_probe_reports_agent_readiness(self, monkeypatch):
        from prospector_app.backend import chat
        monkeypatch.setattr(chat, "readiness",
                            lambda: {"provider": "claude", "ok": True})
        found = onboarding.probe(store_url=None, repo=None, key_file=None,
                                 agent=True)
        assert found["agent"] == {"provider": "claude", "ok": True}

    def test_state_reports_no_choice_until_one_is_made(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        monkeypatch.delenv("TRIAGE_AGENT_PROVIDER", raising=False)
        assert onboarding.state()["agent_provider"] is None

    def test_state_reports_the_recorded_choice(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "none")
        assert onboarding.state()["agent_provider"] == "none"

    def test_the_choice_never_travels_in_a_bundle(self, monkeypatch):
        """Agent auth is the local CLI's login, so each machine picks for
        itself."""
        monkeypatch.setenv("TRIAGE_REPO", "owner/name")
        monkeypatch.setenv("TRIAGE_AGENT_PROVIDER", "claude")
        assert "TRIAGE_AGENT_PROVIDER" not in onboarding.build_bundle()["env"]


class TestReconfigure:
    """Adopting a write rebuilds the store snapshot only when the write moved
    the store: a one-line preference change must not cost a cold reload."""

    @pytest.fixture
    def resets(self, monkeypatch):
        from prospector_app.backend import data
        calls: list[int] = []
        monkeypatch.setattr(data, "reset", lambda: calls.append(1))
        before = dict(os.environ)
        yield calls
        os.environ.clear()
        os.environ.update(before)

    def test_a_new_store_target_resets_the_snapshot(self, resets):
        onboarding.reconfigure({"TRIAGE_REPO": "acme/widgets"})
        assert resets == [1]

    def test_a_new_store_url_resets_the_snapshot(self, resets):
        onboarding.reconfigure({"TRIAGE_STORE_URL": "sqlite:///team.db"})
        assert resets == [1]

    def test_a_preference_leaves_the_snapshot_alone(self, resets):
        onboarding.reconfigure({"TRIAGE_AGENT_PROVIDER": "claude"})
        onboarding.reconfigure({"TRIAGE_BOT_LOGIN": "acme-bot"})
        assert resets == []


class TestStateWhileLoading:
    def test_reports_loading_and_no_counts_until_the_snapshot_lands(self, monkeypatch):
        from prospector_app.backend import data, worker_readiness
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setattr(data, "snapshot_loading", lambda: True)
        monkeypatch.setattr(worker_readiness, "report", lambda: {"ready": False})
        st = onboarding.state()
        assert st["loading"] is True
        assert st["counts"] == {}

    def test_reports_counts_once_loaded(self, monkeypatch):
        from prospector_app.backend import data, worker_readiness
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setattr(data, "snapshot_loading", lambda: False)
        monkeypatch.setattr(data, "prs", lambda: {1: object(), 2: object()})
        monkeypatch.setattr(data, "clusters", lambda: {7: object()})
        monkeypatch.setattr(worker_readiness, "report", lambda: {"ready": False})
        st = onboarding.state()
        assert st["loading"] is False
        assert st["counts"] == {"prs": 2, "clusters": 1}


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
                             "env": {"TRIAGE_REPO": "acme/widgets",
                                     "TRIAGE_BOT_LOGIN": "acme-bot",
                                     "TRIAGE_BOT_APP_ID": "12345"},
                             "profile": PROFILE})
        r = client.post("/api/onboarding/apply",
                        json={"step": "join", "bundle": bundle})
        assert r.status_code == 200
        assert r.json()["configured"] is True

    def test_a_keyed_bundle_files_the_key_over_http(
            self, adopting, keyed, monkeypatch):
        from fastapi.testclient import TestClient
        from prospector_app.backend import app as app_mod
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        bundle = json.dumps({"version": 1,
                             "env": {"TRIAGE_REPO": "acme/widgets",
                                     "TRIAGE_BOT_LOGIN": "acme-bot",
                                     "TRIAGE_BOT_APP_ID": "12345"},
                             "profile": PROFILE, "bot_key_pem": PEM})
        r = client.post("/api/onboarding/apply",
                        json={"step": "join", "bundle": bundle})
        assert r.status_code == 200
        installed = onboarding.KEY_DIR / "acme-bot" / "private-key.pem"
        assert installed.read_text() == PEM
        assert os.environ["TRIAGE_BOT_KEY_FILE"] == str(installed)

    def test_share_includes_the_key_only_when_asked(self, keyed, monkeypatch):
        from fastapi.testclient import TestClient
        from prospector_app.backend import app as app_mod
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setenv("TRIAGE_BOT_LOGIN", "acme-bot")
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        plain = client.post("/api/setup/share", json={}).json()["bundle"]
        assert "bot_key_pem" not in plain
        keyed_bundle = client.post("/api/setup/share",
                                   json={"include_key": True}).json()["bundle"]
        assert json.loads(keyed_bundle)["bot_key_pem"] == PEM

    def test_sharing_a_key_this_machine_lacks_is_a_400(self, monkeypatch):
        from fastapi.testclient import TestClient
        from prospector_app.backend import app as app_mod
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setenv("TRIAGE_BOT_LOGIN", "acme-bot")
        monkeypatch.setenv("TRIAGE_BOT_KEY_FILE", "/nonexistent/key.pem")
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        r = client.post("/api/setup/share", json={"include_key": True})
        assert r.status_code == 400

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
