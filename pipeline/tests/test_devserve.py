"""Port-wait helper: bound port detected, closed port times out; the
reload-dir arguments are ones uvicorn accepts and honours, and they cover the
backend's own source without reaching the rest of the repo root."""
from __future__ import annotations

import socket
import threading
from pathlib import Path

from uvicorn.config import resolve_reload_patterns

from pipeline import devserve
from pipeline import settings


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


def test_await_port_succeeds_on_bound_port():
    server = socket.create_server(("localhost", 0))
    port = server.getsockname()[1]
    accept_thread = threading.Thread(target=lambda: server.accept(), daemon=True)
    accept_thread.start()
    try:
        assert devserve.await_port(port, timeout=5.0) is True
    finally:
        server.close()


def test_await_port_times_out_on_closed_port():
    sock = socket.create_server(("localhost", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert devserve.await_port(port, timeout=1.0) is False


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


def test_the_repo_has_every_reload_dir():
    """Each watched directory exists in this checkout — uvicorn silently drops
    one that does not, and falls back to watching the whole repo root."""
    for d in devserve.reload_dirs(settings.REPO_ROOT):
        assert Path(d).is_dir(), d
