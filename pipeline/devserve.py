"""Dev launcher: uvicorn --reload + the Vite dev server, torn down together.

Refuses to start when the API port is already bound by another process, runs
setup.sh (idempotent), waits for the API port before starting Vite so the
dev server never proxies into a closed socket, and kills both process groups on
exit. Each group runs in its own session, so Ctrl-C, the SIGHUP a closed
terminal tab sends, and SIGTERM reach this process alone, and each of them
stops it through that teardown. uvicorn's --reload worker tree shuts down on
SIGINT but ignores SIGTERM, so teardown sends SIGINT to each group, waits
briefly, then SIGKILLs survivors.
While both run, a child that switches off the terminal's signal keys (raw mode)
has the launcher's terminal settings put back, so Ctrl-C keeps reaching it, and
the processes holding the terminal are named.
Node/pnpm discovery stays in frontend-toolchain.sh, sourced by the bash child.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import termios
import time
from pathlib import Path
from types import FrameType

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

# Ctrl-C, a closed terminal, `kill`.
STOP_SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)

# How long a stopping backend waits for in-flight requests before cancelling
# them. The reloader starts the new server only once the old one exits, and
# no server answers the API until then.
SHUTDOWN_GRACE_SECONDS = 5


def reload_dirs(root: Path) -> list[str]:
    """The `--reload-dir` arguments: each source package, absolute. uvicorn
    watches these trees recursively and reloads on any `*.py` write inside
    them; a directory it cannot resolve is dropped, and an empty set falls
    back to the whole working directory."""
    return [str(root / rel) for rel in SOURCE_DIRS]


def backend_command(root: Path, port: int) -> list[str]:
    return [
        sys.executable, "-m", "uvicorn", "prospector_app.backend.app:app",
        "--port", str(port),
        "--reload",
        *[arg for d in reload_dirs(root) for arg in ("--reload-dir", d)],
        "--timeout-graceful-shutdown", str(SHUTDOWN_GRACE_SECONDS),
    ]


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


def _foreground_terminal() -> int | None:
    """stdin's fd when it is a terminal this launcher runs in the foreground of.
    Setting a terminal's attributes from a background process stops it."""
    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd) and os.tcgetpgrp(fd) == os.getpgrp():
            return fd
    except (OSError, ValueError):
        pass
    return None


def restore_signals(fd: int, saved: list[int | list[bytes | int]]) -> bool:
    """Put `saved` back on terminal `fd` when its signal keys (ISIG) are off.
    True when it had to."""
    if termios.tcgetattr(fd)[3] & termios.ISIG:
        return False
    termios.tcsetattr(fd, termios.TCSANOW, saved)
    return True


def terminal_holders(fd: int) -> list[str]:
    """`pid command` for each other process with terminal `fd` open."""
    try:
        out = subprocess.run(["lsof", "-Fpc", os.ttyname(fd)],
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    holders: list[str] = []
    pid = ""
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c") and pid not in (str(os.getpid()), str(os.getppid())):
            holders.append(f"{pid} {line[1:]}")
    return holders


def _guard_terminal(saved: list[int | list[bytes | int]]) -> None:
    fd = _foreground_terminal()
    if fd is None or not restore_signals(fd, saved):
        return
    print("⚠ a child process switched off Ctrl-C on this terminal; restored it."
          " Processes holding the terminal:", file=sys.stderr)
    for holder in terminal_holders(fd):
        print(f"    {holder}", file=sys.stderr)


def _stop(signum: int, frame: FrameType | None) -> None:
    """The first stop signal ends the supervision loop; the copies that follow
    it (a closed tab under `uv run` delivers several) are ignored, so none of
    them interrupts the teardown."""
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise KeyboardInterrupt


def _teardown(procs: list[subprocess.Popen[bytes]]) -> None:
    # A stop signal during cleanup must not abort it; ignore them in this
    # process while the children are being stopped.
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
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

    for sig in STOP_SIGNALS:
        signal.signal(sig, _stop)
    fd = _foreground_terminal()
    saved = termios.tcgetattr(fd) if fd is not None else None
    procs: list[subprocess.Popen[bytes]] = []
    try:
        print(f"→ backend  http://localhost:{api_port}  (API)")
        backend = subprocess.Popen(
            backend_command(root, api_port),
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
            if saved is not None:
                _guard_terminal(saved)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        _teardown(procs)
        fd = _foreground_terminal()
        if saved is not None and fd is not None:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
