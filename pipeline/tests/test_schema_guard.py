"""Store schema-version write guard: a checkout whose code is behind the
store's stamped schema version can read but not write (#401)."""
import pytest
from sqlalchemy import select

from pipeline import schema, settings, storekit
from pipeline.store import Store
from pipeline.storekit import StaleSchemaError


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def _pr(n=1):
    return {
        "pr": n,
        "meta": {
            "title": "fix: a bug", "state": "open", "head_sha": "abc123",
            "checked_at": "2026-07-09T00:00:00+00:00",
        },
    }


def _stored_version(store):
    """The stamped version, or None when no schema row exists."""
    with store.engine.connect() as conn:
        row = conn.execute(
            select(schema.registries.c.data)
            .where(schema.registries.c.name == "schema")).first()
    return None if row is None else row[0]["version"]


def _plant_version(store, version):
    """Stamp `version` as if written by other code, and re-read the guard."""
    storekit._stamp_schema_version(store.engine, version)
    storekit.refresh_schema_guard(store.engine)


def test_construction_alone_does_not_stamp(store):
    assert _stored_version(store) is None


def test_first_write_stamps_code_version(store):
    store.save_pr(_pr())
    assert _stored_version(store) == schema.STORE_SCHEMA_VERSION


def test_behind_store_refuses_writes_allows_reads(store):
    store.save_pr(_pr(1))
    _plant_version(store, schema.STORE_SCHEMA_VERSION + 1)
    assert store.load_pr(1) is not None
    assert set(store.all_prs()) == {1}
    with pytest.raises(StaleSchemaError, match="pull main"):
        store.save_pr(_pr(2))
    with pytest.raises(StaleSchemaError):
        store.append_run({"phase": "test"})
    with pytest.raises(StaleSchemaError):
        store.save_threats({"actors": {}, "incidents": []})
    assert store.load_pr(2) is None


def test_allow_stale_downgrades_to_warning(store, monkeypatch, capsys):
    monkeypatch.setattr(settings, "STORE_ALLOW_STALE", True)
    _plant_version(store, schema.STORE_SCHEMA_VERSION + 1)
    store.save_pr(_pr(2))
    assert store.load_pr(2) is not None
    assert "behind the store" in capsys.readouterr().err


def test_newer_code_bumps_stamp_on_first_write(store):
    _plant_version(store, 0)
    store.save_pr(_pr(1))
    assert _stored_version(store) == schema.STORE_SCHEMA_VERSION
