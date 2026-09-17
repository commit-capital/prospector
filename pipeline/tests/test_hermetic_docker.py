"""The test session shares the developer's Docker daemon with a live verify
worker, whose pinned base image is one `docker rmi` away."""
from __future__ import annotations

import subprocess

from pipeline import verify_driver, verify_gc


def test_the_launcher_environment_names_no_reachable_daemon():
    host = verify_driver.launcher_env().get("DOCKER_HOST", "")
    assert host.startswith("unix:///nonexistent/")


def test_an_unstubbed_sweep_reclaims_nothing(monkeypatch):
    ran: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        return real_run(cmd, **kw)  # type: ignore[call-overload]

    monkeypatch.setattr(verify_gc.subprocess, "run", recording_run)
    result = verify_gc.collect(None)
    assert result["reclaimed"] == []
    assert not [cmd for cmd in ran if cmd[:2] == ["docker", "rmi"]]
