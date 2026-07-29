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
