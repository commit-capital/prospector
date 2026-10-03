from fastapi.testclient import TestClient

from prospector_app.backend import app as appmod


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


def test_lifespan_detaches_stdin_before_launching_anything(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(appmod.subproc, "detach_stdin", lambda: order.append("detach"))
    monkeypatch.setattr(appmod, "_restore_jobs", lambda: order.append("jobs"))
    monkeypatch.setattr(appmod, "_launch_live_sweep", lambda: None)
    monkeypatch.setattr(appmod, "_launch_verify_worker", lambda: None)

    with TestClient(appmod.app):
        assert order[:2] == ["detach", "jobs"]
