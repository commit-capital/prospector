"""Per-worktree dev ports.

Two worktrees naming the same API/Vite pair means only one app runs at a time.
A worktree is created from a copy of another one's `.env` — that is how the
operator's configuration reaches it — so it arrives naming ports that already
belong to the checkout it was copied from.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "dev-ports.sh"

ENV_BODY = """\
TRIAGE_REPO=owner/name
TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres
"""


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A primary checkout plus one worktree, each with its own `.env`."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", "-b", "main", cwd=primary)
    _git("-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "root", cwd=primary)
    tree = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feature", str(tree), cwd=primary)
    return primary, tree


def claim(root: Path, env_file: Path) -> str:
    """Run the helper the way setup.sh does — from inside the checkout being
    configured — and return what it printed."""
    r = subprocess.run(
        ["bash", "-c",
         f'source "{HELPER}"; claim_dev_ports "{root}" "{env_file}"'],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def ports(env_file: Path) -> tuple[str | None, str | None]:
    api = vite = None
    for line in env_file.read_text().splitlines():
        if line.startswith("API_PORT="):
            api = line.split("=", 1)[1]
        elif line.startswith("VITE_PORT="):
            vite = line.split("=", 1)[1]
    return api, vite


def test_a_worktree_with_no_ports_claims_a_pair(repo):
    primary, tree = repo
    (tree / ".env").write_text(ENV_BODY)
    claim(tree, tree / ".env")
    api, vite = ports(tree / ".env")
    assert api and vite


def test_an_inherited_pair_moves_off_the_primary(repo):
    """A worktree's .env arrives as a byte-copy of the primary's, ports
    included, so the pair it inherits is already taken."""
    primary, tree = repo
    (primary / ".env").write_text(ENV_BODY + "API_PORT=8788\nVITE_PORT=5174\n")
    (tree / ".env").write_text((primary / ".env").read_text())
    claim(tree, tree / ".env")
    assert ports(tree / ".env") != ("8788", "5174")
    assert ports(primary / ".env") == ("8788", "5174")


def test_the_primary_never_yields_its_own_ports(repo):
    primary, tree = repo
    (primary / ".env").write_text(ENV_BODY + "API_PORT=8788\nVITE_PORT=5174\n")
    (tree / ".env").write_text(ENV_BODY + "API_PORT=8788\nVITE_PORT=5174\n")
    claim(primary, primary / ".env")
    assert ports(primary / ".env") == ("8788", "5174")


def test_a_hand_picked_pair_is_left_alone(repo):
    """The comment says "edit to override", so a port nobody else claims stays
    exactly as the operator set it."""
    primary, tree = repo
    (primary / ".env").write_text(ENV_BODY + "API_PORT=8788\nVITE_PORT=5174\n")
    (tree / ".env").write_text(ENV_BODY + "API_PORT=9001\nVITE_PORT=6001\n")
    claim(tree, tree / ".env")
    assert ports(tree / ".env") == ("9001", "6001")


def test_a_moved_worktree_keeps_every_other_line(repo):
    """This file holds the store password."""
    primary, tree = repo
    (primary / ".env").write_text(ENV_BODY + "API_PORT=8788\nVITE_PORT=5174\n")
    (tree / ".env").write_text((primary / ".env").read_text())
    claim(tree, tree / ".env")
    text = (tree / ".env").read_text()
    assert "TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres" in text
    assert "TRIAGE_REPO=owner/name" in text


def test_a_moved_worktree_names_its_ports_once(repo):
    """The file names each port once, so which value applies is unambiguous."""
    primary, tree = repo
    (primary / ".env").write_text(ENV_BODY + "API_PORT=8788\nVITE_PORT=5174\n")
    (tree / ".env").write_text((primary / ".env").read_text())
    claim(tree, tree / ".env")
    text = (tree / ".env").read_text()
    assert text.count("API_PORT=") == 1
    assert text.count("VITE_PORT=") == 1


def test_two_inherited_worktrees_do_not_collide_with_each_other(repo):
    primary, tree = repo
    second = tree.parent / "wt2"
    _git("worktree", "add", "-q", "-b", "second", str(second), cwd=primary)
    (primary / ".env").write_text(ENV_BODY + "API_PORT=8788\nVITE_PORT=5174\n")
    for t in (tree, second):
        (t / ".env").write_text((primary / ".env").read_text())
    claim(tree, tree / ".env")
    claim(second, second / ".env")
    assert ports(tree / ".env") != ports(second / ".env")
    assert ports(tree / ".env") != ("8788", "5174")
    assert ports(second / ".env") != ("8788", "5174")
