import pytest
from pipeline import schema
from pipeline import storekit
from pipeline.storekit import Collection, ValidationError


def _bad(rec):
    if "x" not in rec:
        raise ValidationError("x required")


@pytest.fixture
def coll(tmp_path):
    eng = storekit.get_engine(f"sqlite:///{tmp_path}/t.db")
    schema.METADATA.create_all(eng)
    return Collection(eng, schema.prs, "pr", _ok, lambda r: r, schema.mirror_pr)


def _ok(rec):
    return None


def test_save_load_roundtrip(coll):
    rec = {"pr": 5, "meta": {"state": "open", "head_sha": "h", "updated_at": "t"}, "x": 1}
    coll.save(rec)
    assert coll.load(5) == rec


def test_load_missing_returns_none(coll):
    assert coll.load(999) is None


def test_save_is_upsert(coll):
    coll.save({"pr": 5, "x": 1})
    coll.save({"pr": 5, "x": 2})
    assert coll.load(5)["x"] == 2
    assert len(coll.all()) == 1


def test_all_returns_keyed_dict(coll):
    coll.save({"pr": 1, "x": 1})
    coll.save({"pr": 2, "x": 1})
    assert set(coll.all()) == {1, 2}


def test_edit_missing_raises(coll):
    with pytest.raises(KeyError):
        coll.edit(404)


def test_delete(coll):
    coll.save({"pr": 5, "x": 1})
    coll.delete(5)
    assert coll.load(5) is None


def test_validate_runs_on_save(coll):
    bad = Collection(coll.engine, schema.prs, "pr", _bad, lambda r: r, schema.mirror_pr)
    with pytest.raises(ValidationError):
        bad.save({"pr": 1})


def test_save_many_is_one_statement(coll):
    from sqlalchemy import event
    statements: list[str] = []
    event.listen(
        coll.engine, "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, executemany:
            statements.append(statement))

    coll.save_many([{"pr": 1, "x": 1}, {"pr": 2, "x": 1}, {"pr": 3, "x": 1}])

    writes = [statement for statement in statements if statement.startswith("INSERT INTO prs")]
    assert len(writes) == 1
    assert set(coll.all()) == {1, 2, 3}


def test_save_many_dedups_last_wins(coll):
    coll.save_many([{"pr": 1, "x": 1}, {"pr": 1, "x": 2}])
    assert coll.load(1)["x"] == 2
    assert len(coll.all()) == 1


def _mirror_disposition(coll, pr):
    from sqlalchemy import select
    with coll.engine.connect() as conn:
        return conn.execute(
            select(schema.prs.c.disposition).where(schema.prs.c.pr == pr)).scalar()


def test_save_upsert_overwrites_stale_mirror_columns(coll):
    # An UPSERT must rewrite every column the first save set — a partial update
    # would leave the old disposition mirror behind after it's dropped.
    base = {"pr": 5, "meta": {"state": "open", "head_sha": "h", "updated_at": "t"}}
    coll.save({**base, "analysis": {"disposition": "merge"}})
    assert _mirror_disposition(coll, 5) == "merge"
    coll.save(base)  # re-save without analysis
    assert _mirror_disposition(coll, 5) is None


def test_save_many_upsert_overwrites_stale_mirror_columns(coll):
    base = {"pr": 5, "meta": {"state": "open", "head_sha": "h", "updated_at": "t"}}
    coll.save_many([{**base, "analysis": {"disposition": "merge"}}])
    assert _mirror_disposition(coll, 5) == "merge"
    coll.save_many([base])
    assert _mirror_disposition(coll, 5) is None


def _nullpool_coll(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    eng = create_engine(f"sqlite:///{tmp_path}/np.db", poolclass=NullPool)
    schema.METADATA.create_all(eng)
    return Collection(eng, schema.prs, "pr", _ok, lambda r: r, schema.mirror_pr)


def _connect_counter(engine):
    from sqlalchemy import event
    n = {"c": 0}
    event.listen(engine, "connect", lambda *a: n.__setitem__("c", n["c"] + 1))
    return n


def test_unbound_opens_a_connection_per_operation(tmp_path):
    coll = _nullpool_coll(tmp_path)
    n = _connect_counter(coll.engine)
    for i in range(5):
        coll.save({"pr": i, "x": 1})
    assert n["c"] >= 5  # NullPool: a fresh connection per save


def test_bound_session_reuses_one_connection(tmp_path):
    coll = _nullpool_coll(tmp_path)
    n = _connect_counter(coll.engine)
    with storekit.bound_session(coll.engine):
        for i in range(5):
            coll.save({"pr": i, "x": 1})
        assert coll.load(0)["x"] == 1  # read sees writes on the same connection
    assert n["c"] == 1  # one connection for the whole batch


def test_bound_session_writes_committed_and_visible_after(tmp_path):
    coll = _nullpool_coll(tmp_path)
    with storekit.bound_session(coll.engine):
        coll.save({"pr": 1, "x": 7})
    assert coll.load(1)["x"] == 7  # durable after the batch closes


def test_bound_session_write_autocommits_immediately(tmp_path):
    from sqlalchemy import select
    coll = _nullpool_coll(tmp_path)
    with storekit.bound_session(coll.engine):
        coll.save({"pr": 1, "x": 5})
        # A separate connection sees the row before the batch closes — the write
        # self-committed, holding no open transaction on the bound connection.
        with coll.engine.connect() as other:
            row = other.execute(
                select(schema.prs.c.data).where(schema.prs.c.pr == 1)).first()
    assert row is not None


def test_bound_session_nested_reuses_outer(tmp_path):
    coll = _nullpool_coll(tmp_path)
    n = _connect_counter(coll.engine)
    with storekit.bound_session(coll.engine):
        with storekit.bound_session(coll.engine):
            coll.save({"pr": 1, "x": 1})
        coll.save({"pr": 2, "x": 1})
    assert n["c"] == 1
    assert set(coll.all()) == {1, 2}


def _sever_next_statement(monkeypatch):
    """Make the next Connection.execute die the way a pooler dropping the socket
    mid-query does; later statements run for real."""
    from sqlalchemy import Connection
    from sqlalchemy.exc import DBAPIError
    real = Connection.execute
    calls: list[int] = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            err = DBAPIError(
                "SELECT", None, Exception("SSL connection has been closed unexpectedly"))
            err.connection_invalidated = True
            raise err
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", flaky)


def test_bound_session_read_survives_a_severed_connection(tmp_path, monkeypatch):
    """A bulk read inside a batch — the verify worker's orphan recovery — retries
    on a replacement connection, and the rest of the block runs on it."""
    coll = _nullpool_coll(tmp_path)
    coll.save({"pr": 1, "x": 1})
    with storekit.bound_session(coll.engine):
        _sever_next_statement(monkeypatch)
        assert set(coll.all()) == {1}
        assert coll.load(1)["x"] == 1


# --- bulk reads page; filtered reads stay server-side ----------------------------

def test_all_pages_through_every_row_in_pk_order(coll, monkeypatch):
    """A bulk read runs as a series of bounded statements, so no single statement
    has to ship the whole table inside the server's statement timeout."""
    monkeypatch.setattr(storekit, "BULK_PAGE_ROWS", 2)
    for n in (3, 1, 5, 2, 4):
        coll.save({"pr": n, "x": n})
    assert list(coll.all()) == [1, 2, 3, 4, 5]
    assert coll.all()[4]["x"] == 4


def test_since_full_load_pages_through_every_row(coll, monkeypatch):
    monkeypatch.setattr(storekit, "BULK_PAGE_ROWS", 2)
    for n in range(1, 6):
        coll.save({"pr": n, "x": n})
    records, high = coll.since(None)
    assert set(records) == {1, 2, 3, 4, 5}
    assert high is not None


def test_since_full_load_never_loses_a_write_landing_between_pages(coll, monkeypatch):
    """A row already paged out that is rewritten before the load finishes must
    be picked up by the next incremental read: the watermark a paged full load
    hands back predates every page, so the rewrite sits strictly above it."""
    monkeypatch.setattr(storekit, "BULK_PAGE_ROWS", 2)
    for n in range(1, 6):
        coll.save({"pr": n, "x": n})
    real_read = coll._read
    reads: list[int] = []

    def read_then_rewrite(fn):
        reads.append(1)
        if len(reads) == 3:  # the second page: row 1 was shipped by the first
            coll.save({"pr": 1, "x": "rewritten"})
        return real_read(fn)

    monkeypatch.setattr(coll, "_read", read_then_rewrite)
    records, high = coll.since(None)
    assert len(records) == 5
    later, _ = coll.since(high)
    assert later[1]["x"] == "rewritten"


def test_where_json_returns_only_rows_whose_path_holds_one_of_the_values(coll):
    coll.save({"pr": 1, "x": 1, "fix_request": {"status": "running"}})
    coll.save({"pr": 2, "x": 1, "fix_request": {"status": "pushing"}})
    coll.save({"pr": 3, "x": 1, "fix_request": {"status": "queued"}})
    coll.save({"pr": 4, "x": 1})
    hits = coll.where_json(("fix_request", "status"), ["running", "pushing"])
    assert set(hits) == {1, 2}
    assert hits[1]["fix_request"]["status"] == "running"


def test_where_json_with_no_values_matches_nothing(coll):
    coll.save({"pr": 1, "x": 1, "fix_request": {"status": "running"}})
    assert coll.where_json(("fix_request", "status"), []) == {}
