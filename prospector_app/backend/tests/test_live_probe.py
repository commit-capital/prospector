"""Whether this machine can mint a configured bot token is probed once and
cached for the backend process's whole lifetime (executor.live_possible) —
a key file added, an app installed, or a network blip cleared after that
first probe otherwise never takes effect without a restart. mint_error()
records *why* the last mint failed (get-bot-token.sh's stderr, or a local
reason) instead of discarding it; /api/identities/refresh is what actually
re-probes on demand ("retry live mode" in the app UI)."""
import os
import subprocess

from fastapi.testclient import TestClient

from prospector_app.backend import app as appmod
from prospector_app.backend import caps
from prospector_app.backend import executor


def _reset_caches():
    executor._live_possible = None
    executor._last_mint_error = None
    caps._cache = None


def test_token_script_reads_generic_key_path_config(tmp_path):
    missing = tmp_path / "missing-key.pem"
    env = {
        **os.environ,
        "TRIAGE_REPO": "example/repo",
        "TRIAGE_BOT_APP_ID": "123",
        "TRIAGE_BOT_KEY_FILE": str(missing),
    }
    r = subprocess.run(["bash", str(executor.GET_TOKEN)], capture_output=True,
                       text=True, timeout=10, env=env)
    assert r.returncode != 0
    assert f"TRIAGE_BOT_KEY_FILE is not readable: {missing}" in r.stderr


def test_mint_bot_token_surfaces_missing_key_from_script(monkeypatch):
    _reset_caches()

    class FakeResult:
        stdout = ""
        stderr = "ERROR: TRIAGE_BOT_KEY_FILE is required. Set it in .env.\n"
        returncode = 1
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: FakeResult())
    assert executor.mint_bot_token() is None
    assert "TRIAGE_BOT_KEY_FILE" in executor.mint_error()


def test_mint_bot_token_records_reason_when_script_missing(monkeypatch):
    _reset_caches()
    monkeypatch.setattr(executor, "GET_TOKEN", executor.REPO_ROOT / "nonexistent-script.sh")
    assert executor.mint_bot_token() is None
    assert "not found" in executor.mint_error()


def test_mint_bot_token_surfaces_script_stderr_on_empty_output(monkeypatch):
    """The actual diagnostic (bad app id, no installation, …) lives in
    get-bot-token.sh's stderr — it must reach the operator, not be discarded."""
    _reset_caches()
    class FakeResult:
        stdout = ""
        stderr = "ERROR: GitHub App 123 has no installation on test-owner.\n"
        returncode = 1
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: FakeResult())
    assert executor.mint_bot_token() is None
    assert "no installation on test-owner" in executor.mint_error()


def test_mint_bot_token_falls_back_to_generic_reason_with_no_stderr(monkeypatch):
    _reset_caches()
    class FakeResult:
        stdout = ""
        stderr = ""
        returncode = 7
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: FakeResult())
    assert executor.mint_bot_token() is None
    assert "exited 7" in executor.mint_error()


def test_mint_bot_token_records_subprocess_exception(monkeypatch):
    _reset_caches()
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="get-bot-token.sh", timeout=30)
    monkeypatch.setattr(executor.subprocess, "run", boom)
    assert executor.mint_bot_token() is None
    assert executor.mint_error() == "token helper failed unexpectedly; see server logs"


def test_mint_bot_token_success_clears_prior_error(monkeypatch):
    _reset_caches()
    executor._last_mint_error = "stale reason from a previous failed probe"

    class FakeResult:
        stdout = "ghs_realtoken\n"
        stderr = ""
        returncode = 0
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: FakeResult())
    assert executor.mint_bot_token() == "ghs_realtoken"
    assert executor.mint_error() is None


def test_live_possible_probes_once_and_caches(monkeypatch):
    """The known footgun: a fix made after the first probe (key file appears,
    app gets installed) does not take effect on its own — only refresh_live()
    (driven by the retry endpoint) clears the cache."""
    _reset_caches()
    calls = {"n": 0}

    def fake_mint():
        calls["n"] += 1
        return None
    monkeypatch.setattr(executor, "mint_bot_token", fake_mint)

    assert executor.live_possible() is False
    assert executor.live_possible() is False  # second call: still cached, no re-probe
    assert calls["n"] == 1

    # Simulate the underlying problem being fixed — live_possible() alone
    # doesn't notice.
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    assert executor.live_possible() is False  # stale — still the cached False

    executor.refresh_live()
    assert executor.live_possible() is True  # re-probed after refresh_live()


def test_identities_reports_live_error_alongside_live_possible(monkeypatch):
    _reset_caches()
    monkeypatch.setattr(executor, "mint_bot_token",
                        lambda: (_ for _ in ()).throw(AssertionError("should use cache")))
    executor._live_possible = False
    executor._last_mint_error = "TRIAGE_BOT_KEY_FILE is required. Set it in .env."

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
    executor._last_mint_error = "stale"
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "ghs_realtoken")

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
    monkeypatch.setattr(
        caps, "run",
        lambda *a, **k: type(
            "Result", (), {"returncode": 0, "stdout": "operator\n", "stderr": ""},
        )(),
    )

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
