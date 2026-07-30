"""Cockpit agent memory: durable learnings are persisted and recalled into the
agent's context across sessions."""
import pytest
from sqlalchemy import create_engine

from app.backend import agent_memory
from pipeline import schema


@pytest.fixture(autouse=True)
def tmp_mem(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    schema.METADATA.create_all(eng)
    monkeypatch.setattr(agent_memory, "_TEST_ENGINE", eng)
    yield
    monkeypatch.setattr(agent_memory, "_TEST_ENGINE", None)


def test_add_and_list_roundtrip():
    e = agent_memory.add("Prefer closing dups with a link to the canonical PR", why="keeps authors oriented")
    assert e["id"] and str(e["text"]).startswith("Prefer closing")
    items = agent_memory.entries()
    assert len(items) == 1 and items[0]["why"] == "keeps authors oriented"


def test_add_defaults_to_operator_author():
    e = agent_memory.add("a hand-edited note")
    assert e["author"] == "operator"


def test_add_records_agent_author():
    e = agent_memory.add("learned this myself", author="agent")
    assert e["author"] == "agent"
    assert agent_memory.entries()[0]["author"] == "agent"


def test_add_rejects_empty():
    with pytest.raises(ValueError):
        agent_memory.add("   ")


def test_entries_preserve_insertion_order():
    agent_memory.add("first")
    agent_memory.add("second")
    assert [m["text"] for m in agent_memory.entries()] == ["first", "second"]


def test_context_block_empty_when_no_memory():
    assert agent_memory.context_block() == ""


def test_context_block_formats_feedback_with_why():
    agent_memory.add("Never auto-merge infra PRs", why="CODEOWNERS gate")
    block = agent_memory.context_block()
    assert "REMEMBERED LEARNINGS" in block
    assert "Never auto-merge infra PRs" in block and "(why: CODEOWNERS gate)" in block


def test_context_block_respects_limit():
    for i in range(60):
        agent_memory.add(f"item {i}")
    block = agent_memory.context_block(limit=10)
    assert block.count("\n  - ") == 10
    assert "item 59" in block and "item 49" not in block  # newest 10 kept
