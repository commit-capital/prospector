"""Store contract against a real Postgres (JSONB, create_all, mirror columns,
importer). Skipped unless TEST_POSTGRES_URL is set; CI provides it via a service
container, and a dev can point it at a local Postgres."""
import os

import pytest
from pipeline import schema
from pipeline import storekit
from pipeline.store import Store

PG_URL = os.environ.get("TEST_POSTGRES_URL")
# These share the single Postgres database and drop_all/create_all around every
# test, so under `pytest -n auto` they must run on one worker — otherwise two
# workers drop each other's tables mid-test. xdist_group pins the whole module to
# a single worker; skipif still gates the module off when no PG URL is set.
pytestmark = [
    pytest.mark.skipif(not PG_URL, reason="set TEST_POSTGRES_URL to run"),
    pytest.mark.xdist_group("postgres_backend"),
]


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr("pipeline.settings.STORE_URL", PG_URL)
    eng = storekit.get_engine(PG_URL)
    schema.METADATA.drop_all(eng)
    schema.METADATA.create_all(eng)
    yield Store()
    schema.METADATA.drop_all(eng)


def _pr(n=5):
    return {"pr": n, "meta": {"title": "t", "state": "open", "head_sha": "h",
                              "updated_at": "u", "checked_at": "c"},
            "analysis": {"disposition": "merge", "rationale": "r"}}


def test_pr_roundtrip_on_postgres(store):
    store.save_pr(_pr())
    assert store.load_pr(5).raw == _pr()


def test_mirror_column_queryable_on_postgres(store):
    store.save_pr(_pr(5))
    store.save_pr(dict(_pr(6), analysis={"disposition": "close-stale"}))
    from sqlalchemy import select
    with store.engine.connect() as conn:
        rows = conn.execute(
            select(schema.prs.c.pr).where(schema.prs.c.disposition == "merge")).all()
    assert [r[0] for r in rows] == [5]


def test_runs_and_registry_on_postgres(store):
    store.append_run({"phase": "ingest", "ts": "c"})
    assert [r.raw for r in store.runs()] == [{"phase": "ingest", "ts": "c"}]
    store.save_threats({"actors": {"x": 1}, "incidents": []})
    assert store.load_threats() == {"actors": {"x": 1}, "incidents": []}


def test_importer_into_postgres(tmp_path, monkeypatch):
    from pipeline import store_migrate
    src = tmp_path / "json"
    (src / "prs").mkdir(parents=True)
    import json
    (src / "prs" / "5.json").write_text(json.dumps(_pr()))
    monkeypatch.setattr("pipeline.settings.STORE_URL", PG_URL)
    eng = storekit.get_engine(PG_URL)
    schema.METADATA.drop_all(eng)
    schema.METADATA.create_all(eng)
    store_migrate.import_pr_store(src, store_migrate.dest_store("@env"))
    assert Store().load_pr(5).raw == _pr()
    schema.METADATA.drop_all(eng)
