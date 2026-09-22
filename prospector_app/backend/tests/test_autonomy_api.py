"""The header's autonomy disclosure reads this machine's lane switches over
one route, which serves the writable-flag allowlist and nothing else."""
from __future__ import annotations

from fastapi.testclient import TestClient

from prospector_app.backend import app as appmod
from prospector_app.backend import worker_control


def test_serves_every_writable_flag(monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_AUTOPUSH", "update,rebase")
    monkeypatch.setenv("TRIAGE_VERIFY_WORKER", "1")
    monkeypatch.delenv("TRIAGE_FIX_WORKER", raising=False)
    r = TestClient(appmod.app).get("/api/autonomy")
    assert r.status_code == 200
    flags = r.json()["flags"]
    assert set(flags) == set(worker_control.WRITABLE)
    assert flags["TRIAGE_FIX_AUTOPUSH"] == "update,rebase"
    assert flags["TRIAGE_VERIFY_WORKER"] == "1"
    assert flags["TRIAGE_FIX_WORKER"] == ""


def test_never_carries_a_credential_or_the_store_url(monkeypatch):
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgresql://user:sup3rsecret@host/db")
    monkeypatch.setenv("TRIAGE_BOT_KEY_FILE", "/keys/private-key.pem")
    body = TestClient(appmod.app).get("/api/autonomy").text
    assert "sup3rsecret" not in body
    assert "private-key.pem" not in body
