"""Durable, repo-scoped memory for the cockpit's embedded agent.

The agent pane (chat.py) spins up a fresh Claude per subject; without persistent
memory it re-learns the same repo-specific preferences and corrections each
thread. Learnings are written here and recalled at the start of every new thread,
persisted in the shared SQL store so they survive restarts and are shared across
machines.

Two authors write here: the agent (via agent/remember, author="agent") and the
operator (author="operator").
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, insert, select

from pipeline import schema
from pipeline import storekit

# pipeline/ holds the default SQLite store when TRIAGE_STORE_URL is unset.
_DEFAULT_STORE_ROOT = Path(__file__).resolve().parents[2] / "pipeline" / "store"

# Test hook: monkeypatch.setattr(agent_memory, "_TEST_ENGINE", eng) in fixtures.
_TEST_ENGINE: Engine | None = None
_table_ensured: bool = False


def _engine() -> Engine:
    global _table_ensured
    if _TEST_ENGINE is not None:
        return _TEST_ENGINE
    url = os.environ.get("TRIAGE_STORE_URL") or f"sqlite:///{_DEFAULT_STORE_ROOT}/store.db"
    eng = storekit.get_engine(url)
    if not _table_ensured:
        with eng.begin() as conn:
            schema.agent_memory.create(conn, checkfirst=True)
        _table_ensured = True
    return eng


def entries() -> list[dict[str, object]]:
    """All remembered learnings, oldest-first (by insertion order)."""
    with _engine().connect() as conn:
        rows = conn.execute(
            select(schema.agent_memory).order_by(schema.agent_memory.c.rowid)
        ).mappings().all()
    return [dict(r) for r in rows]


def add(text: str, why: str | None = None, tags: list[str] | None = None,
        author: str = "operator") -> dict[str, object]:
    """Persist a learning. Raises ValueError if text is empty."""
    text = (text or "").strip()
    if not text:
        raise ValueError("memory text is required")
    entry: dict[str, object] = {
        "id": uuid.uuid4().hex[:12],
        "at": datetime.now().isoformat(timespec="seconds"),
        "author": author,
        "text": text,
        "why": (why or "").strip() or None,
        "tags": [t for t in (tags or []) if t],
    }
    with _engine().begin() as conn:
        conn.execute(insert(schema.agent_memory).values(**entry))
    return entry


def context_block(limit: int = 50) -> str:
    """Remembered learnings formatted for injection into the agent's context.
    Empty string when there's nothing to recall."""
    items = entries()[-limit:]
    if not items:
        return ""
    lines = ["REMEMBERED LEARNINGS — durable guidance accumulated across past sessions "
             "about this repo, written by you (when you learned something worth keeping) "
             "or the operator. Apply it unless it conflicts with a direct instruction in "
             "this conversation:"]
    for m in items:
        line = f"  - {m['text']}"
        if m.get("why"):
            line += f"  (why: {m['why']})"
        lines.append(line)
    return "\n".join(lines)
