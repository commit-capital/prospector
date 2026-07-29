"""Store construction reconciles columns only when the declared schema has moved:
the fingerprint stamped in `registries` gates the reflection + DDL that used to run
on every Store() (#460)."""
import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select, text

from pipeline import schema, storekit
from pipeline.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def _stored_fingerprint(store):
    """The stamped fingerprint, or None when no row exists."""
    with store.engine.connect() as conn:
        row = conn.execute(
            select(schema.registries.c.data)
            .where(schema.registries.c.name == "schema_fingerprint")).first()
    return None if row is None else row[0]["fingerprint"]


def _live_columns(store, table="prs"):
    with store.engine.connect() as conn:
        return {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).all()}


def _drop_head_sha(store):
    """Remove a live column behind the store's back — a stand-in for a store that
    predates the column's declaration. head_sha carries no index, so SQLite allows it."""
    with store.engine.begin() as conn:
        conn.execute(text("ALTER TABLE prs DROP COLUMN head_sha"))


def _reconstruct(root):
    """A Store() from a fresh process's point of view: the per-process
    already-ensured cache is what makes a second construction free."""
    storekit._SCHEMAS_ENSURED.clear()
    return Store(root)


def test_construction_stamps_the_declared_fingerprint(store):
    assert _stored_fingerprint(store) == storekit.schema_fingerprint(store.engine)


def test_fingerprint_covers_every_declared_table():
    """The reconcile walks all of METADATA, not just the two Collection tables — so
    a column added to activity or runs is picked up like one added to prs."""
    walked = {t.name for t in schema.METADATA.sorted_tables}
    assert {"prs", "clusters", "issues", "issue_clusters",
            "activity", "runs", "registries"} <= walked


def _fingerprint_of(monkeypatch, engine, metadata):
    monkeypatch.setattr(schema, "METADATA", metadata)
    return storekit.schema_fingerprint(engine)


def test_fingerprint_moves_when_a_column_is_added(store, monkeypatch):
    """The signal that drives the whole gate: an added mirror column — which changes
    no record shape, so STORE_SCHEMA_VERSION has no reason to move — still shifts the
    fingerprint, and so still triggers exactly one reconcile."""
    base = MetaData()
    Table("t", base, Column("id", Integer, primary_key=True), Column("data", String))
    grown = MetaData()
    Table("t", grown, Column("id", Integer, primary_key=True), Column("data", String),
          Column("author", String, index=True))

    assert (_fingerprint_of(monkeypatch, store.engine, base)
            != _fingerprint_of(monkeypatch, store.engine, grown))


def test_fingerprint_is_stable_across_equivalent_declarations(store, monkeypatch):
    """Declaration order must not move the fingerprint, or every construction would
    reconcile on a coin flip."""
    one = MetaData()
    Table("t", one, Column("a", Integer, primary_key=True), Column("b", String))
    two = MetaData()
    Table("t", two, Column("b", String), Column("a", Integer, primary_key=True))

    assert (_fingerprint_of(monkeypatch, store.engine, one)
            == _fingerprint_of(monkeypatch, store.engine, two))


def test_matching_fingerprint_skips_the_reconcile(store, tmp_path):
    """The gate's whole point: with the stamp current, construction does no
    reflection and no DDL — so a column removed behind the store's back stays gone."""
    _drop_head_sha(store)
    assert "head_sha" not in _live_columns(store)

    _reconstruct(tmp_path)
    assert "head_sha" not in _live_columns(store)


def test_stale_fingerprint_reconciles_and_restamps(store, tmp_path):
    """A store whose stamp doesn't match this code's declarations gets its missing
    columns added back, and the fresh fingerprint stamped so the next construction
    is free again."""
    _drop_head_sha(store)
    storekit._stamp_registry(store.engine, "schema_fingerprint",
                             {"fingerprint": "stamped-by-older-code"})

    _reconstruct(tmp_path)
    assert "head_sha" in _live_columns(store)
    assert _stored_fingerprint(store) == storekit.schema_fingerprint(store.engine)


def test_unstamped_store_reconciles(store, tmp_path):
    """A store that predates the fingerprint (no stamp row at all) must still
    reconcile — the never-stamped fallback path."""
    _drop_head_sha(store)
    with store.engine.begin() as conn:
        conn.execute(text("DELETE FROM registries WHERE name = 'schema_fingerprint'"))

    _reconstruct(tmp_path)
    assert "head_sha" in _live_columns(store)


def test_reconcile_is_idempotent(store, tmp_path):
    """Two processes that both see a stale stamp both reconcile — the second finds
    every column already present, adds nothing, and re-stamps the same fingerprint."""
    _drop_head_sha(store)
    storekit._stamp_registry(store.engine, "schema_fingerprint", {"fingerprint": "stale"})

    _reconstruct(tmp_path)
    first = _stored_fingerprint(store)
    storekit._stamp_registry(store.engine, "schema_fingerprint", {"fingerprint": "stale"})
    _reconstruct(tmp_path)

    assert "head_sha" in _live_columns(store)
    assert _stored_fingerprint(store) == first


def test_store_stamped_ahead_is_left_alone(store, tmp_path):
    """A checkout behind the store must not reconcile: its declarations are a subset
    of the store's, and stamping its older fingerprint would make every current
    process reconcile again."""
    _drop_head_sha(store)
    storekit._stamp_schema_version(store.engine, schema.STORE_SCHEMA_VERSION + 1)
    storekit._stamp_registry(store.engine, "schema_fingerprint",
                             {"fingerprint": "stamped-by-newer-code"})

    _reconstruct(tmp_path)
    assert "head_sha" not in _live_columns(store)
    assert _stored_fingerprint(store) == "stamped-by-newer-code"


def test_fresh_store_creates_every_table_and_index(tmp_path):
    """create_all still runs unconditionally, so an empty database comes up complete
    on the first construction — before any fingerprint exists to compare."""
    st = _reconstruct(tmp_path / "fresh")
    with st.engine.connect() as conn:
        tables = {r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")).all()}
        indexes = {r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index'")).all()}
    assert {t.name for t in schema.METADATA.sorted_tables} <= tables
    assert {ix.name for t in schema.METADATA.sorted_tables for ix in t.indexes} <= indexes
