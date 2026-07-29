"""The agent's `remember` CLI — its one self-initiated write. Driven end-to-end
as a subprocess (how the sandboxed agent actually invokes it) against a temp
SQLite store via TRIAGE_STORE_URL, so we never touch the production DB."""
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, select

from pipeline import schema

REMEMBER = Path(__file__).resolve().parents[2] / "agent" / "remember"


def _run(args, tmp_path):
    return subprocess.run(
        [sys.executable, str(REMEMBER), *args],
        env={
            "TRIAGE_STORE_URL": f"sqlite:///{tmp_path}/test.db",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True, text=True,
    )


def _entries(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    with engine.connect() as conn:
        rows = conn.execute(
            select(schema.agent_memory).order_by(schema.agent_memory.c.rowid)
        ).mappings().all()
    return [dict(r) for r in rows]


def test_remember_appends_agent_authored_entry(tmp_path):
    r = _run(["Prefer the local store over gh for ingested PRs",
              "--why", "it's faster and richer"], tmp_path)
    assert r.returncode == 0, r.stderr
    entries = _entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["author"] == "agent"
    assert e["text"] == "Prefer the local store over gh for ingested PRs"
    assert e["why"] == "it's faster and richer"
    assert e["id"] and e["at"]


def test_remember_parses_tags(tmp_path):
    r = _run(["a tagged learning", "--tags", "clustering, dispositions"], tmp_path)
    assert r.returncode == 0, r.stderr
    entries = _entries(tmp_path)
    assert entries[0]["tags"] == ["clustering", "dispositions"]


def test_remember_rejects_empty(tmp_path):
    r = _run(["   "], tmp_path)
    assert r.returncode == 2
