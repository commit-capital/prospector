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
    out = worker_log.read(first["offset"], first["file"])
    assert out["lines"] == ["c", "d"] and out["offset"] == log.stat().st_size


def test_a_partial_last_line_waits_for_its_newline(log):
    log.write_text("a\nhalf")
    first = worker_log.read(None)
    out = worker_log.read(0, first["file"])
    assert out["lines"] == ["a"] and out["offset"] == 2
    with log.open("a") as fh:
        fh.write(" done\n")
    assert worker_log.read(out["offset"], out["file"])["lines"] == ["half done"]


def test_a_rotation_finishes_the_old_file_then_starts_the_new(log):
    log.write_text("a\nb\n")
    first = worker_log.read(None)
    with log.open("a") as fh:
        fh.write("c\n")
    log.replace(log.with_suffix(".log.1"))
    log.write_text("d\n")
    out = worker_log.read(first["offset"], first["file"])
    assert out["lines"] == ["c", "d"] and out["offset"] == log.stat().st_size
    assert out["file"] == worker_log.read(None)["file"] and not out["skipped"]


def test_a_rotated_log_that_regrew_past_the_offset_is_not_skipped(log):
    log.write_text("a\n")
    first = worker_log.read(None)
    with log.open("a") as fh:
        fh.write("b\n")
    log.replace(log.with_suffix(".log.1"))
    log.write_text("c\n" * 10)
    out = worker_log.read(first["offset"], first["file"])
    assert out["lines"] == ["b"] + ["c"] * 10


def test_two_rotations_return_the_tail_marked_skipped(log):
    log.write_text("a\n")
    first = worker_log.read(None)
    for text in ("b\n", "c\n"):
        log.replace(log.with_suffix(".log.1"))
        log.write_text(text)
    out = worker_log.read(first["offset"], first["file"])
    assert out["skipped"] is True and out["lines"] == ["c"]


def test_a_truncated_log_returns_its_tail_marked_skipped(log):
    log.write_text("a\nb\nc\n")
    first = worker_log.read(None)
    with log.open("r+") as fh:
        fh.truncate(0)
        fh.write("d\n")
    out = worker_log.read(first["offset"], first["file"])
    assert out["skipped"] is True and out["lines"] == ["d"]


def test_a_reader_far_behind_finishes_the_old_file_over_several_reads(log):
    log.write_text("")
    first = worker_log.read(None)
    log.write_text("".join(f"old {i}\n" for i in range(40)))
    log.replace(log.with_suffix(".log.1"))
    log.write_text("new\n")
    got, cur = [], first
    for _ in range(20):
        cur = worker_log.read(cur["offset"], cur["file"], max_bytes=64)
        got += cur["lines"]
        if "new" in got:
            break
    assert got == [f"old {i}" for i in range(40)] + ["new"]


def test_a_first_read_on_a_line_boundary_keeps_that_line(log):
    log.write_text("a" * 9 + "\n" + "b\n")
    assert worker_log.read(None, max_bytes=2)["lines"] == ["b"]


def test_no_log_yet_reads_empty(log):
    assert worker_log.read(None) == {"lines": [], "offset": 0, "file": None,
                                     "exists": False, "skipped": False}
    log.write_text("x\n")
    assert worker_log.read(None)["exists"] is True


def test_a_large_gap_is_capped(log):
    log.write_text("x" * 99 + "\n" + "y\n" * 10)
    out = worker_log.read(0, worker_log.read(None)["file"], max_bytes=50)
    assert out["offset"] <= 50 + 2 and out["lines"]


def test_endpoint(log, monkeypatch):
    from prospector_app.backend import app as app_mod
    monkeypatch.setenv("TRIAGE_REPO", "owner/name")
    log.write_text("hello\n")
    r = TestClient(app_mod.app).get("/api/worker/log")
    assert r.status_code == 200 and r.json()["lines"] == ["hello"]
    r = TestClient(app_mod.app).get(
        f"/api/worker/log?after={r.json()['offset']}&file={r.json()['file']}")
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


def test_a_secret_straddling_a_forced_cut_is_not_served(log, monkeypatch):
    monkeypatch.setenv("PROSPECTOR_FEEDBACK_TOKEN", "ghp_feedbacktoken123")
    log.write_text("x" * 40 + "ghp_feedbacktoken123" + "y" * 40 + "\n")
    cur = worker_log.read(None, max_bytes=10_000)
    cur = {"offset": 0, "file": cur["file"]}
    text = []
    for _ in range(100):
        before = cur["offset"]
        cur = worker_log.read(cur["offset"], cur["file"], max_bytes=50)
        text += cur["lines"]
        if cur["offset"] == before:
            break
    joined = "".join(text)
    assert "feedbacktoken" not in joined and "[redacted]" in joined


def test_registry_secrets_tokens_and_keys_are_redacted(log, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc123secret")
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgresql://u:p%40ss@h/x")
    log.write_text("key sk-ant-abc123secret\npassword p@ss\n"
                   "minted ghs_ABCDEFGHIJKLMNOPQRSTUVWX12\n"
                   "-----BEGIN RSA PRIVATE KEY-----\n")
    text = "\n".join(worker_log.read(None)["lines"])
    assert "abc123secret" not in text and "p@ss" not in text
    assert "ghs_ABCDEF" not in text and "PRIVATE KEY-----" not in text


def test_tests_never_install_the_real_tee(monkeypatch):
    import sys

    from prospector_app.backend import cluster_refresh
    monkeypatch.setenv("TRIAGE_CLUSTER_WORKER", "1")
    monkeypatch.setattr(cluster_refresh, "pass_once", lambda: None)
    assert cluster_refresh.startup()
    assert cluster_refresh.shutdown()
    assert not isinstance(sys.stdout, worker_log._Tee)
