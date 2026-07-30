"""Dev launcher: uvicorn --reload + the Vite dev server, torn down together.

Refuses to start when the API port is already bound by another process, runs
setup.sh (idempotent), waits for the API port before starting Vite so the
dev server never proxies into a closed socket, and kills both process groups on
exit. uvicorn's --reload worker tree shuts down on SIGINT but ignores SIGTERM,
so teardown sends SIGINT to each group, waits briefly, then SIGKILLs survivors.
Node/pnpm discovery stays in frontend-toolchain.sh, sourced by the bash child.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from pipeline import settings

FRONTEND_CMD = (
    'FRONTEND="$0/prospector_app/frontend" && source "$0/frontend-toolchain.sh" && cd "$FRONTEND" && exec "${PNPM[@]}" dev'
)


# The packages uvicorn's reloader watches: the backend's own importable
# source. Everything else under the repo root writes *.py without changing what
# the server runs — agent worktrees under .claude/, the virtualenvs, the
# frontend's node_modules — and a restart there costs the verification worker
# its in-flight sandbox run.
SOURCE_DIRS = ("pipeline", "issue_triage", "prospector_app/backend")


def reload_dirs(root: Path) -> list[str]:
    """The `--reload-dir` arguments: each source package, absolute. uvicorn
    watches these trees recursively and reloads on any `*.py` write inside
    them; a directory it cannot resolve is dropped, and an empty set falls
    back to the whole working directory."""
    return [str(root / rel) for rel in SOURCE_DIRS]


def is_port_open(port: int) -> bool:
    """Whether a TCP listener currently answers on `port` at localhost."""
    try:
        with socket.create_connection(("localhost", port), timeout=0.5):
            return True
    except OSError:
        return False


def await_port(port: int, proc: subprocess.Popen[bytes], timeout: float = 30.0) -> bool:
    """Wait for `port` to accept connections, or for `proc` to exit first.

    Polling `proc` alongside the socket means a backend that dies immediately
    is detected as soon as it exits, rather than only after the full timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if is_port_open(port):
            return True
        time.sleep(0.5)
    return False


def _killpg(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def _teardown(procs: list[subprocess.Popen[bytes]]) -> None:
    # A second Ctrl-C during cleanup must not abort it; ignore further
    # interrupts in this process while the children are being stopped.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for proc in procs:
        _killpg(proc, signal.SIGINT)
    deadline = time.monotonic() + 3.0
    for proc in procs:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _killpg(proc, signal.SIGKILL)


def run() -> int:
    root = settings.REPO_ROOT
    setup = subprocess.run(["bash", str(root / "setup.sh")])
    if setup.returncode != 0:
        return setup.returncode
    settings.load_env_file()
    api_port = int(os.environ.get("API_PORT", "8787"))
    vite_port = int(os.environ.get("VITE_PORT", "5173"))

    if is_port_open(api_port):
        print(f"✗ port {api_port} is already in use — another instance may be running.", file=sys.stderr)
        return 1

    procs: list[subprocess.Popen[bytes]] = []
    try:
        print(f"→ backend  http://localhost:{api_port}  (API)")
        backend = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "prospector_app.backend.app:app",
                "--port", str(api_port),
                "--reload",
                *[arg for d in reload_dirs(root) for arg in ("--reload-dir", d)],
            ],
            cwd=root,
            start_new_session=True,
        )
        procs.append(backend)
        if not await_port(api_port, backend):
            print(f"✗ backend never bound :{api_port} — see the traceback above.", file=sys.stderr)
            return 1

        print(f"→ frontend http://localhost:{vite_port}  (open this)")
        frontend = subprocess.Popen(
            ["bash", "-c", FRONTEND_CMD, str(root)],
            start_new_session=True,
        )
        procs.append(frontend)

        # Both servers run until Ctrl-C; if either dies, stop the other and
        # surface its exit code.
        while True:
            for proc in procs:
                code = proc.poll()
                if code is not None:
                    return code
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        _teardown(procs)
