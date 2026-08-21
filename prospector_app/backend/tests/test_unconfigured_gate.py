"""An unconfigured app must refuse to look like a working one.

With no deployment target, data.store() falls back to a local SQLite file and
every list route answers {"items": []} — a Prospector watching an empty
repository is indistinguishable from a Prospector that was never configured.
The gate is the one place that refusal lives.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prospector_app.backend import app as app_mod

GATED = ["/api/clusters", "/api/activity", "/api/setup/readiness"]


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.delenv("TRIAGE_REPO", raising=False)
    return TestClient(app_mod.app, raise_server_exceptions=False)


@pytest.fixture
def configured():
    return TestClient(app_mod.app, raise_server_exceptions=False)


class TestUnconfigured:
    @pytest.mark.parametrize("path", GATED)
    def test_api_routes_refuse(self, unconfigured, path):
        r = unconfigured.get(path)
        assert r.status_code == 503
        assert r.json()["unconfigured"] is True

    def test_meta_is_served_so_the_spa_can_route(self, unconfigured):
        r = unconfigured.get("/api/meta")
        assert r.status_code == 200
        assert r.json()["configured"] is False

    @pytest.mark.xfail(reason="the onboarding route lands in Task 7", strict=True)
    def test_onboarding_state_is_served(self, unconfigured):
        r = unconfigured.get("/api/onboarding/state")
        assert r.status_code == 200
        assert r.json()["configured"] is False


class TestConfigured:
    @pytest.mark.parametrize("path", GATED)
    def test_api_routes_are_served(self, configured, path):
        assert configured.get(path).status_code == 200

    def test_meta_reports_configured(self, configured):
        assert configured.get("/api/meta").json()["configured"] is True
