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
    monkeypatch.setenv("TRIAGE_STORE_URL", PG_URL)
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
    monkeypatch.setenv("TRIAGE_STORE_URL", PG_URL)
    eng = storekit.get_engine(PG_URL)
    schema.METADATA.drop_all(eng)
    schema.METADATA.create_all(eng)
    store_migrate.import_pr_store(src, store_migrate.dest_store("@env"))
    assert Store().load_pr(5).raw == _pr()
    schema.METADATA.drop_all(eng)


def test_concurrent_registry_saves_never_collide_on_postgres(store):
    """Two hosts' heartbeats merge into one registry row at once. Under READ
    COMMITTED a delete-then-insert pair races to a primary-key violation on the
    loser; the save must be one atomic upsert so a concurrent beat is a lost
    update at worst, never an exception. The first writer's transaction is held
    open just after its first write to the row, and the second writer lands
    inside that window."""
    import threading
    import time
    from sqlalchemy import event
    store.save_verify_worker({"host": "seed", "last_beat": "2026-08-21T00:00:00+00:00"})
    first_write = threading.Event()
    errors: list[BaseException] = []

    def hold(conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip().upper()
        if (threading.current_thread().name == "first" and "registries" in statement
                and head.startswith(("DELETE", "INSERT")) and not first_write.is_set()):
            first_write.set()
            time.sleep(1.0)

    def beat(host: str) -> None:
        try:
            if host == "second":
                assert first_write.wait(10.0)
            store.save_verify_worker({"host": host, "last_beat": "2026-08-21T00:00:01+00:00"})
        except BaseException as e:  # noqa: BLE001 - surfaced to the assertion
            errors.append(e)

    event.listen(store.engine, "after_cursor_execute", hold)
    try:
        threads = [threading.Thread(target=beat, args=(h,), name=h)
                   for h in ("first", "second")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        event.remove(store.engine, "after_cursor_execute", hold)
    assert first_write.is_set()
    assert errors == []
    # The second writer's merge may lose the first's entry; the row itself is whole.
    assert set(store.load_verify_worker()["hosts"]) & {"first", "second"}


def test_where_json_filters_on_jsonb_path_on_postgres(store):
    store.save_pr(dict(_pr(5), fix_request={"status": "running", "action": "update"}))
    store.save_pr(dict(_pr(6), fix_request={"status": "queued", "action": "update"}))
    store.save_pr(_pr(7))
    hits = store.prs_matching(("fix_request", "status"), ["running", "pushing"])
    assert set(hits) == {5}
    assert hits[5].fix_request["action"] == "update"


def test_bulk_reads_page_on_postgres(store, monkeypatch):
    monkeypatch.setattr(storekit, "BULK_PAGE_ROWS", 2)
    for n in range(1, 6):
        store.save_pr(_pr(n))
    assert list(store.all_prs()) == [1, 2, 3, 4, 5]
    records, high = store.prs_since(None)
    assert set(records) == {1, 2, 3, 4, 5}
    assert high is not None
    assert store.prs_since(high)[0] == {}
