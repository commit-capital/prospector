"""Port-wait helper: bound port detected, closed port times out, a dead
backend process short-circuits the wait; the reload-exclude argument is one
uvicorn accepts and honours."""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
from pathlib import Path

from uvicorn.config import resolve_reload_patterns

from pipeline import devserve


def _running_proc() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])


def test_is_port_open_true_on_bound_port():
    server = socket.create_server(("localhost", 0))
    port = server.getsockname()[1]
    accept_thread = threading.Thread(target=lambda: server.accept(), daemon=True)
    accept_thread.start()
    try:
        assert devserve.is_port_open(port) is True
    finally:
        server.close()


def test_is_port_open_false_on_closed_port():
    sock = socket.create_server(("localhost", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert devserve.is_port_open(port) is False


def test_await_port_succeeds_on_bound_port():
    server = socket.create_server(("localhost", 0))
    port = server.getsockname()[1]
    accept_thread = threading.Thread(target=lambda: server.accept(), daemon=True)
    accept_thread.start()
    proc = _running_proc()
    try:
        assert devserve.await_port(port, proc, timeout=5.0) is True
    finally:
        server.close()
        proc.kill()
        proc.wait()


def test_await_port_times_out_on_closed_port():
    sock = socket.create_server(("localhost", 0))
    port = sock.getsockname()[1]
    sock.close()
    proc = _running_proc()
    try:
        assert devserve.await_port(port, proc, timeout=1.0) is False
    finally:
        proc.kill()
        proc.wait()


def test_await_port_returns_false_as_soon_as_proc_exits():
    sock = socket.create_server(("localhost", 0))
    port = sock.getsockname()[1]
    sock.close()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert devserve.await_port(port, proc, timeout=30.0) is False


def test_reload_exclude_is_an_existing_directory(tmp_path: Path):
    exclude = devserve.reload_exclude(tmp_path)
    assert Path(exclude).is_dir()


def test_uvicorn_excludes_the_reload_exclude_directory(tmp_path: Path, monkeypatch):
    """uvicorn resolves the argument to a watched-path exclusion. A path it
    cannot resolve to a directory is globbed against the working directory,
    where an absolute pattern is rejected."""
    monkeypatch.chdir(tmp_path)
    exclude = devserve.reload_exclude(tmp_path)
    _patterns, directories = resolve_reload_patterns([exclude], [])
    assert Path(exclude).resolve() in directories
