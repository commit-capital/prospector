"""The worker lanes' subprocesses, which outlive the backend that started them.

A dev-server reload or a crash ends the backend and leaves its children running
against requests the next backend marks interrupted or re-queues. `spawn`
records each child with the pid of the backend that started it, and `reap`
ends the process tree of every recorded child whose backend has exited.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from prospector_app.backend.jobs import REPO_ROOT

CHILDREN_DIR = REPO_ROOT / "prospector_app" / "cache" / "children"
GRACE_SECONDS = 10.0


@contextmanager
def spawn(argv: Sequence[str], *,
          env: Mapping[str, str] | None = None) -> Iterator[subprocess.Popen[str]]:
    """Run `argv` from REPO_ROOT with stdout and stderr merged into one text
    pipe, recorded while the block runs."""
    command = [str(a) for a in argv]
    proc = subprocess.Popen(command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env)
    path = CHILDREN_DIR / f"{proc.pid}.json"
    CHILDREN_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"owner": os.getpid(), "pid": proc.pid,
                                "command": " ".join(command)}))
    try:
        yield proc
    finally:
        path.unlink(missing_ok=True)


def reap() -> list[int]:
    """End every recorded child whose backend has exited, and return their
    pids. A record whose pid now runs another command is dropped unsignalled."""
    if not CHILDREN_DIR.is_dir():
        return []
    ended: list[int] = []
    for path in sorted(CHILDREN_DIR.glob("*.json")):
        try:
            rec = json.loads(path.read_text())
            owner, pid, command = int(rec["owner"]), int(rec["pid"]), str(rec["command"])
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            continue
        if _alive(owner):
            continue
        if _runs(pid, command):
            _end_tree(pid)
            ended.append(pid)
        path.unlink(missing_ok=True)
    return ended


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _runs(pid: int, command: str) -> bool:
    if not _alive(pid):
        return False
    try:
        out = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip() == command


def _tree(root: int) -> tuple[set[int], set[int]]:
    """`root` and its descendants, and the process groups a descendant leads.
    The root shares the group of the supervisor that started the backend, so
    the root's own group is never signalled; a headless agent or a sandbox
    launcher leads a group of its own."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="],
                         capture_output=True, text=True, timeout=10).stdout
    children: dict[int, list[int]] = {}
    group_of: dict[int, int] = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) != 3 or not all(f.isdigit() for f in fields):
            continue
        pid, parent, group = (int(f) for f in fields)
        children.setdefault(parent, []).append(pid)
        group_of[pid] = group
    pids: set[int] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        pids.add(pid)
        stack.extend(children.get(pid, []))
    return pids, {pid for pid in pids - {root} if group_of.get(pid) == pid}


def _signal(pids: set[int], groups: set[int], sig: signal.Signals) -> None:
    for group in groups:
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError):
            pass
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _end_tree(root: int) -> None:
    """SIGTERM the tree, then SIGKILL whatever is left after GRACE_SECONDS."""
    try:
        pids, groups = _tree(root)
    except (OSError, subprocess.SubprocessError):
        pids, groups = {root}, set()
    _signal(pids, groups, signal.SIGTERM)
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.2)
    _signal({p for p in pids if _alive(p)}, groups, signal.SIGKILL)
