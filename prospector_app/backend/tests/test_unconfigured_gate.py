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
        assert r.status_code == 409
        assert r.json()["unconfigured"] is True

    @pytest.mark.parametrize("path", GATED)
    def test_the_refusal_is_never_read_as_an_outage(self, unconfigured, path):
        """The frontend covers the page with a "can't reach the backend" banner
        on 502/503/504. A checkout waiting to be set up is not an outage."""
        assert unconfigured.get(path).status_code not in (502, 503, 504)

    def test_meta_is_served_so_the_spa_can_route(self, unconfigured):
        r = unconfigured.get("/api/meta")
        assert r.status_code == 200
        assert r.json()["configured"] is False

    def test_health_is_served_so_the_app_does_not_look_offline(self, unconfigured):
        """The frontend treats 503 as the backend being unreachable, so gating
        the liveness probe puts a "can't reach the backend" banner over a
        backend that is running fine."""
        r = unconfigured.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_health_does_not_read_the_store_when_unconfigured(
            self, unconfigured, monkeypatch):
        """Refusing /api/clusters while health reports counts from that same
        store would be the gate leaking through its own exemption."""
        def boom():
            raise AssertionError("the snapshot was loaded on an unconfigured app")
        monkeypatch.setattr(app_mod.data, "clusters", boom)
        monkeypatch.setattr(app_mod.data, "prs", boom)
        assert unconfigured.get("/api/health").json()["clusters"] == 0

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
