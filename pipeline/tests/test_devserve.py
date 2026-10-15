"""Port-wait helper: bound port detected, closed port times out, a dead
backend process short-circuits the wait; the reload-dir arguments are ones
uvicorn accepts and honours, and they cover the backend's own source without
reaching the rest of the repo root; the backend's graceful shutdown is bounded;
Ctrl-C, a closed terminal and SIGTERM each stop both servers."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from uvicorn.config import resolve_reload_patterns
from uvicorn.main import main as uvicorn_main

from pipeline import devserve
from pipeline import settings

# Runs devserve.run() over a scratch root (argv[1]) with stand-in servers: a
# backend that binds the API port, and a frontend that sleeps. Each records
# its pid in the root.
SUPERVISOR = """
import sys
from pathlib import Path
from pipeline import devserve, settings

root = Path(sys.argv[1])
settings.REPO_ROOT = root
backend = (
    "import os, socket, sys, time; "
    "s = socket.create_server(('localhost', int(sys.argv[1]))); "
    "open(sys.argv[2], 'w').write(str(os.getpid())); "
    "time.sleep(60)"
)
devserve.backend_command = lambda _root, port: [
    sys.executable, "-c", backend, str(port), str(root / "backend.pid")]
devserve.FRONTEND_CMD = 'echo $$ > "$0/frontend.pid" && exec sleep 60'
sys.exit(devserve.run())
"""


def _watched(root: Path) -> list[Path]:
    """The directories uvicorn resolves from devserve's --reload-dir
    arguments — the roots of the trees a write can restart the server from."""
    _patterns, directories = resolve_reload_patterns([], devserve.reload_dirs(root))
    return directories


def _reloads_on(root: Path, path: Path) -> bool:
    """Whether a write to `path` restarts the server: uvicorn watches each
    resolved reload dir recursively and matches every event under it against
    its `*.py` include."""
    return path.match("*.py") and any(d in path.parents for d in _watched(root))


def _tree(root: Path) -> None:
    """Lay out the source packages devserve watches — uvicorn keeps only
    reload dirs that exist."""
    for rel in ("pipeline", "issue_triage", "prospector_app/backend"):
        (root / rel).mkdir(parents=True, exist_ok=True)


def _running_proc() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])


def _free_port() -> int:
    with socket.create_server(("localhost", 0)) as sock:
        return sock.getsockname()[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pids(root: Path, timeout: float) -> list[int]:
    """The stand-in servers' pids, once both have started."""
    files = [root / "backend.pid", root / "frontend.pid"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        texts = [f.read_text().strip() if f.exists() else "" for f in files]
        if all(texts):
            return [int(t) for t in texts]
        time.sleep(0.1)
    raise AssertionError("the stand-in servers never started")


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


def test_uvicorn_watches_every_reload_dir(tmp_path: Path, monkeypatch):
    """uvicorn resolves each argument to a watched directory. A list it
    resolves to nothing falls back to the whole working directory, so the
    watched set must come back exactly as passed."""
    monkeypatch.chdir(tmp_path)
    _tree(tmp_path)
    assert sorted(_watched(tmp_path)) == sorted(
        Path(d).resolve() for d in devserve.reload_dirs(tmp_path))


def test_the_backend_source_reloads(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tree(tmp_path)
    for rel in ("pipeline/gates.py", "issue_triage/triage.py",
                "prospector_app/backend/app.py"):
        assert _reloads_on(tmp_path, tmp_path / rel), rel


def test_an_agent_worktree_does_not_reload(tmp_path: Path, monkeypatch):
    """A checkout under .claude/worktrees/ is a whole second copy of this repo
    inside the watched root. Its sources — and the virtualenv an agent syncs
    there — must not restart the server: a restart kills the verification
    worker's in-flight sandbox run, which then needs a manual re-queue."""
    monkeypatch.chdir(tmp_path)
    _tree(tmp_path)
    tree = tmp_path / ".claude" / "worktrees" / "some-branch"
    for rel in ("pipeline/gates.py", "prospector_app/backend/app.py",
                ".venv/lib/python3.14/site-packages/anyio/abc.py"):
        assert not _reloads_on(tmp_path, tree / rel), rel


def test_neither_the_venv_nor_the_frontend_reloads(tmp_path: Path, monkeypatch):
    """The repo root also holds trees that churn *.py without being backend
    source: the virtualenv, and a node_modules package that ships Python."""
    monkeypatch.chdir(tmp_path)
    _tree(tmp_path)
    for rel in (".venv/lib/python3.14/site-packages/anyio/abc.py",
                "prospector_app/frontend/node_modules/flatted/python/flatted.py"):
        assert not _reloads_on(tmp_path, tmp_path / rel), rel


def test_the_backend_bounds_its_graceful_shutdown():
    """A request still running when a reload stops the old server is
    cancelled after the grace period, so the reloader can start the new one."""
    cmd = devserve.backend_command(settings.REPO_ROOT, 8787)
    assert cmd[1:3] == ["-m", "uvicorn"]
    params = uvicorn_main.make_context("uvicorn", cmd[3:]).params
    assert params["reload"] is True
    assert params["timeout_graceful_shutdown"] == devserve.SHUTDOWN_GRACE_SECONDS


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGHUP, signal.SIGTERM],
                         ids=["ctrl-c", "tab-close", "kill"])
def test_a_stop_signal_stops_both_servers(tmp_path: Path, sig: signal.Signals):
    """Each server runs in its own session, so a signal meant for the dev
    server — Ctrl-C, the hangup a closed terminal tab sends, a plain `kill` —
    reaches the supervisor alone, which must stop both on the way out. Under
    `uv run` a closed tab delivers a burst: the shell's hangup, the
    terminal's, and the copies uv forwards, all within a millisecond."""
    (tmp_path / "setup.sh").write_text("exit 0\n")
    env = {**os.environ, "API_PORT": str(_free_port()), "VITE_PORT": str(_free_port()),
           "TRIAGE_SKIP_DOTENV": "1"}
    with (tmp_path / "supervisor.log").open("wb") as log:
        supervisor = subprocess.Popen(
            [sys.executable, "-c", SUPERVISOR, str(tmp_path)],
            cwd=settings.REPO_ROOT, env=env, stdout=log, stderr=log,
        )
    pids: list[int] = []
    try:
        pids = _pids(tmp_path, timeout=30.0)
        burst_end = time.monotonic() + 0.005
        while time.monotonic() < burst_end:
            try:
                os.kill(supervisor.pid, sig)
            except ProcessLookupError:
                break
        supervisor.wait(timeout=10)
        deadline = time.monotonic() + 5.0
        while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert [pid for pid in pids if _alive(pid)] == [], (tmp_path / "supervisor.log").read_text()
        assert supervisor.returncode == 0
    finally:
        supervisor.kill()
        for pid in pids:
            if _alive(pid):
                os.killpg(pid, signal.SIGKILL)


def test_the_repo_has_every_reload_dir():
    """Each watched directory exists in this checkout — uvicorn silently drops
    one that does not, and falls back to watching the whole repo root."""
    for d in devserve.reload_dirs(settings.REPO_ROOT):
        assert Path(d).is_dir(), d
