from fastapi.testclient import TestClient

from review_cockpit.backend import app as appmod


def test_lifespan_launches_background_services(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(
        appmod, "_launch_live_sweep", lambda: launched.append("live-sweep")
    )
    monkeypatch.setattr(
        appmod, "_launch_verify_worker", lambda: launched.append("verify-worker")
    )

    with TestClient(appmod.app):
        assert launched == ["live-sweep", "verify-worker"]
