"""The header's autonomy disclosure endpoint: the lane switches, nothing else.

`.env` also holds the store password and both credential paths, so the test
pins that the response is exactly the writable lane switches."""
from __future__ import annotations

from fastapi.testclient import TestClient

from prospector_app.backend import app as appmod
from prospector_app.backend import worker_control


def test_autonomy_returns_every_lane_switch(monkeypatch):
    monkeypatch.setenv("TRIAGE_REPO", "owner/name")
    monkeypatch.setenv("TRIAGE_FIX_AUTOPUSH", "update,rebase")
    monkeypatch.delenv("TRIAGE_VERIFY_AUTOHUNT", raising=False)
    r = TestClient(appmod.app).get("/api/autonomy")
    assert r.status_code == 200
    flags = r.json()["flags"]
    assert set(flags) == set(worker_control.WRITABLE)
    assert flags["TRIAGE_FIX_AUTOPUSH"] == "update,rebase"
    assert flags["TRIAGE_VERIFY_AUTOHUNT"] == ""


def test_autonomy_carries_no_deployment_secrets(monkeypatch):
    monkeypatch.setenv("TRIAGE_REPO", "owner/name")
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgresql://user:s3kretpw@host/db")
    monkeypatch.setenv("TRIAGE_BOT_KEY_FILE", "/keys/private-key.pem")
    r = TestClient(appmod.app).get("/api/autonomy")
    assert r.status_code == 200
    assert "s3kretpw" not in r.text
    assert "private-key" not in r.text
