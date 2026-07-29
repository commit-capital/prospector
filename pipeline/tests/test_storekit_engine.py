"""URL resolution + engine cache for the SQL backend."""
import pytest

from pipeline import storekit
from sqlalchemy import Engine
from sqlalchemy.pool import NullPool


def test_resolve_url_from_path(tmp_path):
    url = storekit.resolve_url(tmp_path)
    assert url == f"sqlite:///{tmp_path}/store.db"


def test_resolve_url_none_uses_settings(monkeypatch):
    monkeypatch.setattr("pipeline.settings.STORE_URL", "postgresql+psycopg://x/y")
    assert storekit.resolve_url(None, default_path="/tmp/d") == "postgresql+psycopg://x/y"


def test_resolve_url_none_falls_back_to_default_path(monkeypatch):
    monkeypatch.setattr("pipeline.settings.STORE_URL", None)
    assert storekit.resolve_url(None, default_path="/tmp/d") == "sqlite:////tmp/d/store.db"


def test_engine_is_cached_per_url(tmp_path):
    e1 = storekit.get_engine(f"sqlite:///{tmp_path}/a.db")
    e2 = storekit.get_engine(f"sqlite:///{tmp_path}/a.db")
    e3 = storekit.get_engine(f"sqlite:///{tmp_path}/b.db")
    assert isinstance(e1, Engine) and e1 is e2 and e1 is not e3


def test_postgres_url_engine_uses_nullpool():
    # Built without connecting (lazy). Networked URLs hold no idle connection —
    # NullPool opens per operation — so many clients share a small pooler budget.
    eng = storekit.get_engine("postgresql+psycopg://u:p@localhost:5432/doesnotconnect")
    assert isinstance(eng.pool, NullPool)


def test_unsupported_store_dialect_is_rejected():
    with pytest.raises(
        ValueError,
        match="unsupported store database 'mysql'; use SQLite or PostgreSQL",
    ):
        storekit.get_engine("mysql://u:p@localhost/db")
