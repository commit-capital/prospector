from __future__ import annotations

import json
import subprocess
import sys
import time

from prospector_app.backend import worker_children

# A child that starts a grandchild in a session of its own, the way a headless
# agent or a sandbox launcher is started, prints its pid, and sleeps.
TREE = ("import subprocess, sys, time; "
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "start_new_session=True); print(g.pid, flush=True); time.sleep(60)")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while worker_children._alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not worker_children._alive(pid)


def test_spawn_records_the_child_while_it_runs():
    with worker_children.spawn([sys.executable, "-c", "print('hi')"]) as proc:
        path = worker_children.CHILDREN_DIR / f"{proc.pid}.json"
        assert json.loads(path.read_text())["pid"] == proc.pid
        assert proc.stdout is not None and proc.stdout.read() == "hi\n"
        proc.wait()
    assert not path.exists()


def test_reap_ends_the_tree_of_a_child_whose_backend_exited(monkeypatch):
    monkeypatch.setattr(worker_children, "GRACE_SECONDS", 2.0)
    argv = [sys.executable, "-c", TREE]
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None
    grandchild = int(child.stdout.readline())
    worker_children.CHILDREN_DIR.mkdir(parents=True)
    record = worker_children.CHILDREN_DIR / f"{child.pid}.json"
    record.write_text(json.dumps({"owner": _dead_pid(), "pid": child.pid,
                                  "command": " ".join(argv)}))
    try:
        assert worker_children.reap() == [child.pid]
        child.wait(timeout=5)
        assert _gone(grandchild)
        assert not record.exists()
    finally:
        child.kill()


def test_reap_leaves_a_live_backends_child_alone():
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    child = subprocess.Popen(argv)
    worker_children.CHILDREN_DIR.mkdir(parents=True)
    record = worker_children.CHILDREN_DIR / f"{child.pid}.json"
    record.write_text(json.dumps({"owner": child.pid, "pid": child.pid,
                                  "command": " ".join(argv)}))
    try:
        assert worker_children.reap() == []
        assert child.poll() is None
        assert record.exists()
    finally:
        child.kill()
        child.wait()


def test_reap_drops_a_record_whose_pid_runs_another_command():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    worker_children.CHILDREN_DIR.mkdir(parents=True)
    record = worker_children.CHILDREN_DIR / f"{child.pid}.json"
    record.write_text(json.dumps({"owner": _dead_pid(), "pid": child.pid,
                                  "command": "uv run python -u pipeline/verify_pr.py"}))
    try:
        assert worker_children.reap() == []
        assert child.poll() is None
        assert not record.exists()
    finally:
        child.kill()
        child.wait()
