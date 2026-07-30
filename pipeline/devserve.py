"""Cockpit dev launcher: uvicorn --reload + the Vite dev server, torn down together.

Runs setup.sh (idempotent), waits for the API port before starting Vite so the
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
    'FRONTEND="$0/review_cockpit/frontend" && source "$0/frontend-toolchain.sh" && cd "$FRONTEND" && exec "${PNPM[@]}" dev'
)


def reload_exclude(root: Path) -> str:
    """The `--reload-exclude` argument: the cockpit's diff cache, absolute.

    uvicorn keeps the exclusion as a directory only when the argument names an
    existing directory, and matches it against the absolute paths it watches,
    so the cache directory is created here before the server starts. The
    cockpit itself fills it lazily on its first diff fetch.
    """
    path = root / "review_cockpit" / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def await_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=0.5):
                return True
        except OSError:
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

    procs: list[subprocess.Popen[bytes]] = []
    try:
        print(f"→ backend  http://localhost:{api_port}  (API)")
        backend = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "review_cockpit.backend.app:app",
                "--port", str(api_port),
                "--reload",
                "--reload-exclude", reload_exclude(root),
            ],
            cwd=root,
            start_new_session=True,
        )
        procs.append(backend)
        if not await_port(api_port):
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
