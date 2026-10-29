"""Whether this machine can mint a configured bot token is probed once and
cached for the backend process's whole lifetime (executor.live_possible) —
a key file added, an app installed, or a network blip cleared after that
first probe otherwise never takes effect without a restart.
/api/identities/refresh is what actually re-probes on demand ("retry live
mode" in the app UI)."""
from fastapi.testclient import TestClient

from pipeline import bot_token
from prospector_app.backend import app as appmod
from prospector_app.backend import caps
from prospector_app.backend import executor


def _reset_caches():
    executor._live_possible = None
    bot_token._last_error = None
    executor._bot_permissions = None
    executor._bot_permissions_probed = False
    caps._cache = None


def test_bot_permissions_uses_nonsecret_token_helper_mode_and_caches(monkeypatch):
    _reset_caches()
    calls = []

    class FakeResult:
        stdout = '{"actions":"write","issues":"write"}\n'
        stderr = ""
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append(command)
        return FakeResult()

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    assert executor.bot_permissions() == {"actions": "write", "issues": "write"}
    assert executor.bot_permissions() == {"actions": "write", "issues": "write"}
    assert calls == [["bash", str(bot_token.GET_TOKEN), "--permissions"]]


def test_bot_permissions_fails_closed_on_malformed_output(monkeypatch):
    _reset_caches()

    class FakeResult:
        stdout = "not-json\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(executor.subprocess, "run", lambda *args, **kwargs: FakeResult())
    assert executor.bot_permissions() is None


def test_live_possible_probes_once_and_caches(monkeypatch):
    """The known footgun: a fix made after the first probe (key file appears,
    app gets installed) does not take effect on its own — only refresh_live()
    (driven by the retry endpoint) clears the cache."""
    _reset_caches()
    calls = {"n": 0}

    def fake_mint():
        calls["n"] += 1
        return None
    monkeypatch.setattr("pipeline.bot_token.get", fake_mint)

    assert executor.live_possible() is False
    assert executor.live_possible() is False  # second call: still cached, no re-probe
    assert calls["n"] == 1

    # Simulate the underlying problem being fixed — live_possible() alone
    # doesn't notice.
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "tok")
    assert executor.live_possible() is False  # stale — still the cached False

    executor.refresh_live()
    assert executor.live_possible() is True  # re-probed after refresh_live()


def test_identities_reports_live_error_alongside_live_possible(monkeypatch):
    _reset_caches()
    monkeypatch.setattr(bot_token, "get",
                        lambda: (_ for _ in ()).throw(AssertionError("should use cache")))
    executor._live_possible = False
    bot_token._last_error = "TRIAGE_BOT_KEY_FILE is required. Set it in .env."

    out = executor.identities()
    assert out["live_possible"] is False
    assert out["live_error"] == "TRIAGE_BOT_KEY_FILE is required. Set it in .env."
    assert "TRIAGE_BOT_KEY_FILE is required" in out["identities"][0]["note"]


def test_identities_omits_error_when_live(monkeypatch):
    _reset_caches()
    executor._live_possible = True
    out = executor.identities()
    assert out["live_error"] is None


def test_identities_reports_the_push_lane(monkeypatch, tmp_path):
    # The contributor-push user is the second write identity — separate from
    # the bot App, with its own availability — so the payload names it too.
    _reset_caches()
    executor._live_possible = True
    key = tmp_path / "push-key"
    key.write_text("key\n")
    monkeypatch.setenv("TRIAGE_PUSH_LOGIN", "test-push-bot")
    monkeypatch.setenv("TRIAGE_PUSH_EMAIL", "1+test-push-bot@users.noreply.github.com")
    monkeypatch.setenv("TRIAGE_PUSH_SSH_KEY_FILE", str(key))
    out = executor.identities()
    assert out["push"] == {"login": "test-push-bot", "available": True}


def test_identities_reports_an_unconfigured_push_lane(monkeypatch):
    _reset_caches()
    executor._live_possible = True
    monkeypatch.delenv("TRIAGE_PUSH_LOGIN", raising=False)
    monkeypatch.delenv("TRIAGE_PUSH_EMAIL", raising=False)
    monkeypatch.delenv("TRIAGE_PUSH_SSH_KEY_FILE", raising=False)
    out = executor.identities()
    assert out["push"] == {"login": None, "available": False}
    assert out["identities"][0]["note"] is None


def test_refresh_route_reprobes_and_returns_fresh_identities(monkeypatch):
    """The known footgun end to end: a stale cached False survives until the
    operator hits the retry action, which is this route."""
    _reset_caches()
    executor._live_possible = False
    bot_token._last_error = "stale"
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "ghs_realtoken")

    c = TestClient(appmod.app)
    r = c.post("/api/identities/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["live_possible"] is True
    assert body["live_error"] is None
    _reset_caches()


def test_capabilities_disable_writes_when_server_schema_is_stale(monkeypatch):
    _reset_caches()
    executor._live_possible = True
    monkeypatch.setattr(
        caps, "store_schema_status",
        lambda: {
            "code_version": 12,
            "store_version": 13,
            "write_block": "stale server",
        },
    )
    monkeypatch.setattr(caps.gh, "operator_login", lambda **k: "operator")

    out = caps.capabilities()

    assert out["merge_upstream"] is False
    assert out["alerts"]["available"] is False
    assert out["write_block"] == "stale server"
    assert out["store_schema"] == {
        "code_version": 12,
        "store_version": 13,
        "write_block": "stale server",
    }
    _reset_caches()
