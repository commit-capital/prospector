"""Reading this machine's worker log from an offset, across a rotation."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prospector_app.backend import worker_log


@pytest.fixture
def log(tmp_path, monkeypatch):
    f = tmp_path / "logs" / "worker-mac.log"
    f.parent.mkdir()
    monkeypatch.setattr(worker_log, "path", lambda: f)
    return f


def test_a_first_read_returns_the_tail_and_its_offset(log):
    log.write_text("".join(f"line {i}\n" for i in range(500)))
    out = worker_log.read(None, tail_lines=3)
    assert out["lines"] == ["line 497", "line 498", "line 499"]
    assert out["offset"] == log.stat().st_size


def test_a_read_from_an_offset_returns_only_what_was_appended(log):
    log.write_text("a\nb\n")
    first = worker_log.read(None)
    with log.open("a") as fh:
        fh.write("c\nd\n")
    out = worker_log.read(first["offset"])
    assert out["lines"] == ["c", "d"] and out["offset"] == log.stat().st_size


def test_a_partial_last_line_waits_for_its_newline(log):
    log.write_text("a\nhalf")
    out = worker_log.read(0)
    assert out["lines"] == ["a"] and out["offset"] == 2
    with log.open("a") as fh:
        fh.write(" done\n")
    assert worker_log.read(out["offset"])["lines"] == ["half done"]


def test_a_rotation_finishes_the_old_file_then_starts_the_new(log):
    log.write_text("a\nb\n")
    offset = worker_log.read(None)["offset"]
    with log.open("a") as fh:
        fh.write("c\n")
    log.replace(log.with_suffix(".log.1"))
    log.write_text("d\n")
    out = worker_log.read(offset)
    assert out["lines"] == ["c", "d"] and out["offset"] == log.stat().st_size


def test_no_log_yet_reads_empty(log):
    assert worker_log.read(None) == {"lines": [], "offset": 0, "exists": False}
    log.write_text("x\n")
    assert worker_log.read(None)["exists"] is True


def test_a_large_gap_is_capped(log):
    log.write_text("x" * 99 + "\n" + "y\n" * 10)
    out = worker_log.read(0, max_bytes=50)
    assert out["offset"] <= 50 + 2 and out["lines"]


def test_endpoint(log, monkeypatch):
    from prospector_app.backend import app as app_mod
    monkeypatch.setenv("TRIAGE_REPO", "owner/name")
    log.write_text("hello\n")
    r = TestClient(app_mod.app).get("/api/worker/log")
    assert r.status_code == 200 and r.json()["lines"] == ["hello"]
    r = TestClient(app_mod.app).get(f"/api/worker/log?after={r.json()['offset']}")
    assert r.json()["lines"] == []


def test_deployment_secrets_never_leave_in_a_line(log, monkeypatch):
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgresql+psycopg://user:s3kretpw@db.example/x")
    monkeypatch.setenv("PROSPECTOR_FEEDBACK_TOKEN", "ghp_feedbacktoken123")
    monkeypatch.setenv("TRIAGE_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/zzz")
    log.write_text("connect postgresql+psycopg://user:s3kretpw@db.example/x failed\n"
                   "auth s3kretpw rejected\ntoken ghp_feedbacktoken123\n"
                   "post https://hooks.slack.com/services/T/B/zzz\n")
    text = "\n".join(worker_log.read(None)["lines"])
    assert "s3kretpw" not in text and "ghp_feedbacktoken123" not in text and "zzz" not in text
    assert text.count("[redacted]") == 4


@pytest.mark.parametrize("module", ["issue_fix_worker", "cluster_refresh"])
def test_every_lane_mirrors_its_output_to_the_log(module, monkeypatch):
    import importlib
    import threading
    mod = importlib.import_module(f"prospector_app.backend.{module}")
    installed = []
    monkeypatch.setattr(worker_log, "install", lambda: installed.append(1))
    monkeypatch.setattr(mod, "enabled", lambda: True)
    monkeypatch.setattr(mod, "running", lambda: False)

    class Idle:
        def __init__(self, *a, **k) -> None: ...
        def start(self) -> None: ...
        def is_alive(self) -> bool: return False
    monkeypatch.setattr(threading, "Thread", Idle)
    assert mod.startup() is True
    assert installed == [1]
