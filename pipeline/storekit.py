"""Generic validated-store + freshness primitives shared by the PR store
(store.py / freshness.py / model.py) and the issue store
(issue_triage/issue_store.py).

A Collection is one SQL table of validated JSON records keyed by an integer id
column. The freshness engine (stamp / is_current_core) is parametric over which
"token" field a fact is bound to — against_head_sha for PRs, against_updated_at
for issues — so one implementation serves both entity families.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar
from collections.abc import Callable, Generator

from sqlalchemy import (
    ARRAY, Connection, Engine, Table, Text, cast, create_engine,
    delete as sa_delete, func, inspect, select, text, type_coerce,
    update as sa_update,
)
from sqlalchemy.dialects.postgresql import Insert as PGInsert, insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert, insert as sqlite_insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import ColumnElement

T = TypeVar("T")
R = TypeVar("R")

_ENGINES: dict[str, Engine] = {}
_ENGINES_LOCK = threading.Lock()
_SCHEMAS_ENSURED: set[str] = set()
_SCHEMAS_LOCK = threading.Lock()


def resolve_url(root: Path | str | None, default_path: Path | str | None = None) -> str:
    """The SQLAlchemy URL a store should use. An explicit `root` always maps to a
    local SQLite file under it (tests + CLI --store, never the shared DB). A None
    root consults settings.STORE_URL, then falls back to a SQLite file under
    `default_path` (the store's default root)."""
    if root is not None:
        return f"sqlite:///{Path(root)}/store.db"
    from pipeline import settings
    if settings.STORE_URL:
        return settings.STORE_URL
    if default_path is None:
        raise ValueError("resolve_url: default_path required when root and STORE_URL are both unset")
    return f"sqlite:///{Path(default_path)}/store.db"


def get_engine(url: str) -> Engine:
    """One engine per URL, process-wide.

    SQLite URLs get the default pool plus a busy timeout, and their parent
    directory is created if absent. PostgreSQL URLs use NullPool — a connection
    is opened per operation and closed on release, never held idle — so several
    clients (each app, reload worker, and pipeline run) share a small
    connection budget without exhausting it. NullPool keeps those clients from
    pinning idle connections and consuming the shared pooler's slots. psycopg's
    server-side prepared statements are disabled so the engine works through a
    transaction-mode pooler, which rotates the backend connection between
    statements. connect_timeout bounds each attempt so a flaky path fails fast.
    Other SQLAlchemy dialects are rejected because the store's upserts and JSON
    expressions are implemented only for SQLite and PostgreSQL."""
    backend = make_url(url).get_backend_name()
    if backend not in {"sqlite", "postgresql"}:
        raise ValueError(
            f"unsupported store database {backend!r}; use SQLite or PostgreSQL"
        )
    eng = _ENGINES.get(url)
    if eng is None:
        with _ENGINES_LOCK:
            eng = _ENGINES.get(url)
            if eng is None:
                if backend == "sqlite":
                    if url.startswith("sqlite:///"):
                        Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
                    eng = create_engine(url, future=True, connect_args={"timeout": 30})
                else:
                    eng = create_engine(
                        url, future=True, poolclass=NullPool,
                        connect_args={"connect_timeout": 10, "prepare_threshold": None})
                _ENGINES[url] = eng
    return eng


def schema_fingerprint(engine: Engine) -> str:
    """A digest of the physical schema this code declares: every table, each of its
    columns by name and compiled type, and each of its indexes by name and columns.
    Compiled against `engine`'s dialect, so a store's fingerprint is comparable only
    to one taken against that same store."""
    from pipeline import schema
    parts: list[str] = []
    for table in schema.METADATA.sorted_tables:
        for col in sorted(table.columns, key=lambda c: c.name):
            parts.append(f"{table.name}.{col.name} {col.type.compile(engine.dialect)}")
        for ix in sorted(table.indexes, key=lambda i: i.name or ""):
            cols = ",".join(sorted(c.name for c in ix.columns))
            parts.append(f"{table.name}!{ix.name} ({cols})")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _stamped_fingerprint(engine: Engine) -> str | None:
    """The fingerprint the store was stamped with. None when the store has never been
    stamped — including when `registries` itself doesn't exist yet, which is how a
    never-created database answers. The read runs in AUTOCOMMIT, so the failed
    lookup doesn't poison a transaction."""
    try:
        return (_read_registry(engine, "schema_fingerprint") or {}).get("fingerprint")
    except DBAPIError:
        return None


def ensure_schema(engine: Engine) -> None:
    """Bring the live database up to the schema this code declares, once per engine
    URL in this process: create absent tables, then reconcile every table's columns
    and indexes.

    Both halves are gated on `schema_fingerprint` — a digest of every declared table,
    column, and index, stamped in the store's `schema_fingerprint` registry row. When
    the stamp matches, the live schema is already exactly what this code expects, so
    construction costs one row read: no create_all existence walk, no reflection, no
    DDL. Against a networked pooler that is the difference between one connection and
    a dozen, and the handshake dominates everything else. Any change to a table
    definition moves the fingerprint, so exactly one construction reconciles and
    re-stamps; concurrent constructions are safe because ensure_columns is itself
    idempotent and race-tolerant.

    A store stamped at a schema version newer than this code's is left untouched: its
    columns are a superset of what this code declares, and stamping this checkout's
    older fingerprint would only make the next current process reconcile again."""
    key = engine.url.render_as_string(hide_password=False)
    if key in _SCHEMAS_ENSURED:
        return
    with _SCHEMAS_LOCK:
        if key in _SCHEMAS_ENSURED:
            return
        from pipeline import schema
        with bound_session(engine) as conn:
            fingerprint = schema_fingerprint(engine)
            if _stamped_fingerprint(engine) != fingerprint:
                schema.METADATA.create_all(conn)
                enable_rls(engine, conn)
                if not _store_schema_ahead(engine):
                    for table in schema.METADATA.sorted_tables:
                        ensure_columns(engine, table)
                    _stamp_registry(engine, "schema_fingerprint",
                                    {"fingerprint": fingerprint, "stamped_at": now()})
        _SCHEMAS_ENSURED.add(key)


def enable_rls(engine: Engine, conn: Connection) -> None:
    """Turn Row-Level Security on for every store table (Postgres only, no-op on
    SQLite). The store's tables live in the `public` schema, which Supabase exposes
    through its anon-key REST API; RLS-off there means anyone with the project URL
    can read and write the store over that API. Enabling RLS with no policies denies
    the anon/authenticated API roles outright while leaving the pipeline untouched —
    it connects as the `postgres` table owner, which bypasses RLS on its own tables.
    ENABLE ROW LEVEL SECURITY is idempotent, so re-running against an already-secured
    table is harmless."""
    if engine.dialect.name != "postgresql":
        return
    from pipeline import schema
    for table in schema.METADATA.sorted_tables:
        conn.execute(text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY'))


def saved_at_now() -> str:
    """The change-watermark stamp: a microsecond-resolution ISO timestamp. Finer
    than now() (seconds) so a bulk save doesn't stamp many rows with one value and
    bloat the `saved_at >= watermark` boundary refetch."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# -- Runs ledger records ------------------------------------------------------
# The `runs` table holds exactly two record shapes, discriminated on read by
# parse_run and enforced on write (both stores' append_run parse before
# inserting, so the ledger only ever holds parseable rows).

@dataclass(frozen=True)
class PhaseRun:
    """A phase execution (ingest, cluster:commit, security:review-one, …).
    Payload keys vary by phase (stats, applied, pr, trigger, …) and stay in
    `raw` — the record exactly as stored, which dumps round-trip losslessly."""
    phase: str
    started: str | None
    finished: str | None
    raw: dict


@dataclass(frozen=True)
class StoreEdit:
    """A store_edit.py audit entry: one applied transform over a table, with
    the pre-image backup path and the touched record ids."""
    transform: str
    table: str
    examined: int
    changed: list[int]
    deleted: list[int]
    backup: str
    ts: str
    raw: dict


RunRecord = PhaseRun | StoreEdit


def parse_run(record: dict) -> RunRecord:
    """The typed view of one runs-ledger record. A record with a non-empty
    `phase` string is a PhaseRun; one with `action == "store-edit"` is a
    StoreEdit. Anything else — including a malformed record of either kind —
    raises ValueError."""
    phase = record.get("phase")
    if isinstance(phase, str) and phase:
        started = record.get("started")
        finished = record.get("finished")
        if not ((started is None or isinstance(started, str))
                and (finished is None or isinstance(finished, str))):
            raise ValueError(f"phase-run timestamps must be strings: keys={sorted(record)}")
        return PhaseRun(phase=phase, started=started, finished=finished, raw=record)
    if record.get("action") == "store-edit":
        transform = record.get("transform")
        table = record.get("table")
        examined = record.get("examined")
        changed = record.get("changed")
        deleted = record.get("deleted")
        backup = record.get("backup")
        ts = record.get("ts")
        if not (isinstance(transform, str) and isinstance(table, str)
                and isinstance(examined, int)
                and isinstance(changed, list) and isinstance(deleted, list)
                and all(isinstance(n, int) for n in changed + deleted)
                and isinstance(backup, str) and isinstance(ts, str)):
            raise ValueError(f"malformed store-edit record: keys={sorted(record)}")
        return StoreEdit(transform=transform, table=table, examined=examined,
                         changed=changed, deleted=deleted, backup=backup, ts=ts,
                         raw=record)
    raise ValueError(
        f"runs-ledger record is neither a phase run nor a store-edit: keys={sorted(record)}")


def ensure_columns(engine: Engine, table: Table) -> None:
    """Add any column present in `table`'s definition but missing from the live
    table — the additive half of schema evolution that create_all (which only
    creates absent tables) doesn't cover. Idempotent and safe to race: a column
    another process just added reads back as present, and a losing ALTER is
    swallowed. A freshly-added `saved_at` is backfilled so every existing row has a
    watermark and an incremental reader doesn't full-scan until the first write.

    Reflection and index DDL run on the bound_session connection when one is active,
    so a reconcile checks out one connection rather than one per table and index."""
    def live_columns() -> set[str]:
        return {c["name"] for c in inspect(_bound_conn(engine) or engine).get_columns(table.name)}

    existing = live_columns()
    for col in table.columns:
        if col.name in existing:
            continue
        coltype = col.type.compile(engine.dialect)
        try:
            with engine.begin() as conn:
                # Bound the ADD COLUMN's wait for its ACCESS EXCLUSIVE lock so a
                # long-running transaction can't hang a schema reconcile forever;
                # fail fast and surface it instead.
                if engine.dialect.name == "postgresql":
                    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {col.name} {coltype}'))
                if col.name == "saved_at":
                    conn.execute(text(f'UPDATE "{table.name}" SET saved_at = :ts'),
                                 {"ts": saved_at_now()})
        except DBAPIError:
            # Swallow only if the column is now present (another process won the
            # race). Otherwise — a lock timeout or real failure — re-raise so a
            # blocked migration is loud, not a process running on a broken schema.
            if col.name not in live_columns():
                raise
    # Indexes declared on the table but absent live (ADD COLUMN doesn't create the
    # column's index) — so the saved_at watermark filter is an index scan, and a
    # migrated table matches a create_all'd one.
    bind = _bound_conn(engine) or engine
    for ix in table.indexes:
        try:
            ix.create(bind, checkfirst=True)
        except DBAPIError:
            pass  # raced with another process creating it


def read_retrying(engine: Engine, fn: Callable[[Connection], R]) -> R:
    """Run fn(conn) on a fresh connection, retrying once if the first attempt
    dies because the connection was severed mid-query — a NAT, VPN, or pooler
    drop that SQLAlchemy reports as connection_invalidated (and that pre-ping,
    which only validates at checkout, cannot catch). For reads only: nothing is
    half-applied, so re-running on a new connection is safe. A non-disconnect
    error propagates immediately.

    Reads run in AUTOCOMMIT so each statement holds its lock only while it runs:
    a reader that stalls or is killed afterward leaves no open transaction, so it
    can't strand a server session `idle in transaction` (which would pin a pooler
    backend and hold AccessShareLock, blocking DDL)."""
    def connect() -> Connection:
        return engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        with connect() as conn:
            return fn(conn)
    except DBAPIError as exc:
        if not exc.connection_invalidated:
            raise
    with connect() as conn:
        return fn(conn)


class StaleSchemaError(RuntimeError):
    """The store's stamped schema version is newer than this code's — writes
    are refused so a checkout behind main cannot mutate records it
    mishandles."""


class ForeignRepoError(RuntimeError):
    """This process's TRIAGE_REPO differs from the repo the store was stamped
    with. Records naming a PR or issue number carry no repo, so a write from a
    process pointed at another repo is indistinguishable from a real one."""


_SCHEMA_VERSIONS: dict[str, int] = {}      # engine URL -> store's stamped version
_SCHEMA_VERSIONS_LOCK = threading.Lock()
_STALE_WARNED: set[str] = set()            # engine URLs already warned about


def _engine_key(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _read_registry(engine: Engine, name: str) -> dict | None:
    """The `registries` row named `name`, or None when it doesn't exist. Runs on the
    bound_session connection when one is active, so a stamp's read never opens a
    second connection mid-batch."""
    from pipeline import schema

    def q(conn: Connection):
        return conn.execute(
            select(schema.registries.c.data)
            .where(schema.registries.c.name == name)).first()
    conn = _bound_conn(engine)
    row = (bound_read_retrying(engine, conn, q) if conn is not None
           else read_retrying(engine, q))
    return row[0] if row is not None else None


def _stamp_registry(engine: Engine, name: str, data: dict) -> None:
    """Upsert the `registries` row named `name` — direct SQL, so a stamp never
    passes through the write guard (the schema stamp is what the guard reads)."""
    from pipeline import schema
    ins = (pg_insert(schema.registries) if engine.dialect.name == "postgresql"
           else sqlite_insert(schema.registries))
    stmt = ins.values(name=name, data=data).on_conflict_do_update(
        index_elements=[schema.registries.c.name], set_={"data": data})
    conn = _bound_conn(engine)
    if conn is not None:
        conn.execute(stmt)
    else:
        with engine.begin() as c:
            c.execute(stmt)


def _read_store_schema_version(engine: Engine) -> int:
    """The store's stamped schema version; 0 when no stamp row exists."""
    return int((_read_registry(engine, "schema") or {}).get("version", 0))


def _store_schema_ahead(engine: Engine) -> bool:
    """True when the store was stamped by code newer than this checkout."""
    from pipeline import schema
    return _read_store_schema_version(engine) > schema.STORE_SCHEMA_VERSION


def refresh_schema_guard(engine: Engine) -> int:
    """Read and cache the store's stamped schema version for the write guard.
    Called at store construction; assert_writable consults (and maintains) the
    cache afterward."""
    v = _read_store_schema_version(engine)
    with _SCHEMA_VERSIONS_LOCK:
        _SCHEMA_VERSIONS[_engine_key(engine)] = v
    return v


def _stamp_schema_version(engine: Engine, version: int) -> None:
    """Upsert the registries row named 'schema' with `version`."""
    _stamp_registry(engine, "schema", {"version": version, "stamped_at": now()})


def assert_writable(engine: Engine) -> None:
    """The store write guard. When the store's stamped schema version is ahead
    of this code's schema.STORE_SCHEMA_VERSION, raise StaleSchemaError (or,
    with TRIAGE_STORE_ALLOW_STALE=1, warn once per engine). When this code is
    ahead, stamp the store forward. Every record, ledger, and registry write
    calls this first."""
    from pipeline import schema, settings
    key = _engine_key(engine)
    store_v = _SCHEMA_VERSIONS.get(key)
    if store_v is None:
        store_v = refresh_schema_guard(engine)
    code_v = schema.STORE_SCHEMA_VERSION
    if store_v > code_v:
        msg = (f"store schema is v{store_v} but this code is v{code_v} — this "
               f"checkout is behind the store; pull main before writing "
               f"(TRIAGE_STORE_ALLOW_STALE=1 overrides)")
        if not settings.STORE_ALLOW_STALE:
            raise StaleSchemaError(msg)
        if key not in _STALE_WARNED:
            _STALE_WARNED.add(key)
            print(f"WARNING: {msg}", file=sys.stderr)
        return
    if store_v < code_v:
        _stamp_schema_version(engine, code_v)
        with _SCHEMA_VERSIONS_LOCK:
            _SCHEMA_VERSIONS[key] = code_v


def stamped_repo(engine: Engine) -> str | None:
    """The repo the store was stamped with, or None when no stamp row exists."""
    return (_read_registry(engine, "repo") or {}).get("repo") or None


def _stamp_repo(engine: Engine, repo: str) -> None:
    """Upsert the registries row named 'repo'."""
    _stamp_registry(engine, "repo", {"repo": repo, "stamped_at": now()})


def assert_repo(engine: Engine) -> None:
    """Refuse a write when settings.REPO differs from the store's stamped repo,
    stamping an unstamped store on the way through. An activity row records a bare
    PR/issue number, so a process pointed at a scratch repo — an end-to-end test
    driving the real executor — would otherwise write rows indistinguishable from
    real ones. TRIAGE_STORE_ALLOW_FOREIGN_REPO=1 downgrades this to a warning."""
    from pipeline import settings
    stamped = stamped_repo(engine)
    if stamped is None:
        _stamp_repo(engine, settings.REPO)
        return
    if stamped == settings.REPO:
        return
    msg = (f"this process targets {settings.REPO!r} but the store is stamped "
           f"{stamped!r} — refusing to write rows that would be indistinguishable "
           f"from {stamped!r}'s (TRIAGE_STORE_ALLOW_FOREIGN_REPO=1 overrides)")
    if not settings.STORE_ALLOW_FOREIGN_REPO:
        raise ForeignRepoError(msg)
    key = _engine_key(engine)
    if key not in _FOREIGN_WARNED:
        _FOREIGN_WARNED.add(key)
        print(f"WARNING: {msg}", file=sys.stderr)


_FOREIGN_WARNED: set[str] = set()

_BOUND = threading.local()


def _bound_conn(engine: Engine) -> Connection | None:
    """The connection bound to `engine` for this thread, or None outside a
    `bound_session`."""
    return getattr(_BOUND, "conns", {}).get(id(engine))


@contextmanager
def bound_session(engine: Engine) -> Generator[Connection]:
    """Route this thread's Collection ops on `engine` through one reused connection
    for the block's duration. The connection runs in AUTOCOMMIT, so each op is a
    single self-committing statement — writes (single-statement UPSERTs) commit as
    they go, a crash keeps completed rows, and reads add no BEGIN/COMMIT. Dropping
    the per-statement connect handshake dominates cost against a networked pooler.
    Reentrant: a nested call reuses the outer connection and leaves closing to it."""
    conns = getattr(_BOUND, "conns", None)
    if conns is None:
        conns = {}
        _BOUND.conns = conns
    existing = conns.get(id(engine))
    if existing is not None:
        yield existing
        return
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    conns[id(engine)] = conn
    try:
        yield conn
    finally:
        # Close whatever is bound at exit: a rebind may have replaced the
        # connection opened here, and the replacement is the live one.
        conns.pop(id(engine)).close()


def _rebind(engine: Engine) -> Connection:
    """Replace this thread's bound connection on `engine` with a fresh one and
    return it. The caller holds a connection the server severed; every later op
    in the block would fail on it, so it is discarded and its replacement takes
    over the binding."""
    conns = _BOUND.conns
    conns.pop(id(engine)).close()
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    conns[id(engine)] = conn
    return conn


def bound_read_retrying(engine: Engine, conn: Connection,
                        fn: Callable[[Connection], R]) -> R:
    """Run a read on `bound_session`'s reused connection, retrying once on a
    replacement if the server severs it mid-query — the same NAT, VPN, or pooler
    drop `read_retrying` covers on an unbound read, and safe on the same grounds:
    nothing is half-applied, so re-running is safe. The replacement stays bound,
    so the rest of the block continues instead of failing on an invalidated
    connection. A non-disconnect error propagates immediately, binding intact."""
    try:
        return fn(conn)
    except DBAPIError as exc:
        if not exc.connection_invalidated:
            raise
    return fn(_rebind(engine))


def strip_json_paths(data_col: ColumnElement, dialect: str,
                     paths: list[tuple[str, ...]]) -> ColumnElement:
    """A SQL expression for `data_col` with each nested JSON path removed, so a
    bulk read drops a heavy field server-side instead of shipping it over the
    wire. Postgres uses the jsonb `#-` operator; SQLite uses json_remove (coerced
    back to the JSON type so the driver still deserializes the result to a dict).
    Any other dialect returns the column untouched — the strip is an egress
    optimization, not a correctness requirement."""
    if dialect == "postgresql":
        expr: ColumnElement = data_col
        for p in paths:
            expr = expr.op("#-")(cast(list(p), ARRAY(Text())))
        return expr
    if dialect == "sqlite":
        expr = data_col
        for p in paths:
            expr = func.json_remove(expr, "$." + ".".join(p))
        return type_coerce(expr, data_col.type)
    return data_col


class ValidationError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


_WARNED_UNKNOWN_SECTIONS: set[tuple[str, str]] = set()


def warn_unknown_section(table: str, key: str) -> None:
    """Print a once-per-process stderr notice that a record in `table` carries a
    top-level section this code doesn't know. The record round-trips with the
    section preserved; the notice flags that this checkout may be behind the
    store schema."""
    mark = (table, key)
    if mark in _WARNED_UNKNOWN_SECTIONS:
        return
    _WARNED_UNKNOWN_SECTIONS.add(mark)
    print(f"store: unknown section {key!r} in {table} preserved — "
          f"this checkout may be behind the store schema", file=sys.stderr)


class Collection(Generic[T]):
    """One SQL table of validated JSON records keyed by an integer id column.

    engine     the SQLAlchemy engine the table lives in
    table      the SQLAlchemy Table; its `data` column holds the full record and
               its primary key column holds the id
    id_field   the record key holding the id ("pr" or "id"); also the PK column name
    validate   called on every save; raises ValidationError on a bad record
    view       wraps a raw record dict into its bound domain object
    mirror     record -> dict of hot column values written alongside `data`
    """

    def __init__(self, engine: Engine, table: Table, id_field: str,
                 validate: Callable[[dict], None], view: Callable[[dict], T],
                 mirror: Callable[[dict], dict]):
        self.engine = engine
        self.table = table
        self.id_field = id_field
        self.pk = table.c[id_field]
        self.validate = validate
        self.view = view
        self.mirror = mirror

    def _read(self, fn: Callable[[Connection], R]) -> R:
        """Run a read. On a bound_session connection it runs directly on that reused
        AUTOCOMMIT connection — a single round-trip, no BEGIN/COMMIT; otherwise on a
        fresh AUTOCOMMIT connection. Either way a mid-query connection drop is
        retried once on a new connection."""
        conn = _bound_conn(self.engine)
        if conn is not None:
            return bound_read_retrying(self.engine, conn, fn)
        return read_retrying(self.engine, fn)

    def _write(self, fn: Callable[[Connection], R]) -> R:
        """Run a write. On a bound_session connection it runs directly on that
        reused AUTOCOMMIT connection, so each single-statement UPSERT self-commits;
        otherwise in a fresh transaction on its own connection."""
        assert_writable(self.engine)
        conn = _bound_conn(self.engine)
        if conn is not None:
            return fn(conn)
        with self.engine.begin() as c:
            return fn(c)

    def load(self, i: int) -> T | None:
        def q(conn: Connection):
            return conn.execute(
                select(self.table.c.data).where(self.pk == int(i))).first()
        row = self._read(q)
        return None if row is None else self.view(row[0])

    def _row(self, rec: dict) -> dict:
        self.validate(rec)
        return dict(self.mirror(rec), data=rec, saved_at=saved_at_now())

    def _insert(self) -> PGInsert | SQLiteInsert:
        """A dialect INSERT construct exposing on_conflict_do_update — Postgres for
        the shared pooler, SQLite for local/test stores."""
        if self.engine.dialect.name == "postgresql":
            return pg_insert(self.table)
        return sqlite_insert(self.table)

    def _upsert(self, conn: Connection, rows: list[dict]) -> None:
        """Insert-or-overwrite each row in one `INSERT … ON CONFLICT (pk) DO UPDATE`
        statement. Every non-PK column present is overwritten from the incoming row
        (`_row` always sets every mirror column plus data + saved_at), so a conflict
        can't leave a stale mirror. One atomic statement per chunk — correct under
        the bound session's autocommit and a single round-trip."""
        ins = self._insert().values(rows)
        update = {name: ins.excluded[name] for name in rows[0] if name != self.id_field}
        stmt = ins.on_conflict_do_update(index_elements=[self.pk], set_=update)
        conn.execute(stmt)

    def save(self, rec_or_view: dict | T) -> None:
        rec = rec_or_view if isinstance(rec_or_view, dict) else rec_or_view.raw  # type: ignore[union-attr]
        row = self._row(rec)
        self._write(lambda conn: self._upsert(conn, [row]))

    def save_many(self, recs: list[dict]) -> None:
        rows = [self._row(r) for r in recs]
        if not rows:
            return
        by_id = {row[self.id_field]: row for row in rows}  # last occurrence wins
        deduped = list(by_id.values())
        self._write(lambda conn: self._upsert(conn, deduped))

    def stamped(self, i: int) -> tuple[dict, str | None] | None:
        """The raw record plus its saved_at write-stamp, or None when absent —
        the read half of a save_if compare-and-swap."""
        def q(conn: Connection):
            return conn.execute(
                select(self.table.c.data, self.table.c.saved_at)
                .where(self.pk == int(i))).first()
        row = self._read(q)
        return None if row is None else (row[0], row[1])

    def save_if(self, rec: dict, expected_saved_at: str | None) -> bool:
        """Validated conditional overwrite: write `rec` only while the row's
        saved_at still equals `expected_saved_at`, and report whether the write
        landed. Every save changes the stamp, so False means another writer got
        there first and the caller's read is stale — a dialect-agnostic
        compare-and-swap on the write-stamp."""
        row = self._row(rec)
        values = {k: v for k, v in row.items() if k != self.id_field}
        def w(conn: Connection) -> bool:
            res = conn.execute(
                sa_update(self.table)
                .where(self.pk == row[self.id_field],
                       self.table.c.saved_at == expected_saved_at)
                .values(**values))
            return res.rowcount == 1
        return self._write(w)

    def edit(self, i: int) -> T:
        v = self.load(i)
        if v is None:
            raise KeyError(f"{self.id_field} {i} not in {self.table.name}")
        return v

    def delete(self, i: int) -> None:
        self._write(lambda conn: conn.execute(sa_delete(self.table).where(self.pk == int(i))))

    def delete_many(self, ids: list[int]) -> None:
        """Delete every row whose id is in `ids` — one statement for the whole set."""
        vals = sorted({int(i) for i in ids})
        if not vals:
            return
        self._write(lambda conn: conn.execute(
            sa_delete(self.table).where(self.pk.in_(vals))))

    def all(self, omit_paths: list[tuple[str, ...]] | None = None) -> dict[int, T]:
        """Every record, keyed by id. `omit_paths` drops the named nested JSON
        paths from each record server-side (e.g. ('meta', 'body')) so the bulk
        read never ships a heavy field the caller doesn't need — the omitted
        value reads back as absent and is fetched on demand elsewhere."""
        data_col = self.table.c.data
        if omit_paths:
            data_col = strip_json_paths(data_col, self.engine.dialect.name, omit_paths)
        def q(conn: Connection):
            return conn.execute(select(self.pk, data_col).order_by(self.pk)).all()
        rows = self._read(q)
        return {r[0]: self.view(r[1]) for r in rows}

    def since(self, watermark: str | None,
              omit_paths: list[tuple[str, ...]] | None = None) -> tuple[dict[int, T], str | None]:
        """Records written strictly after `watermark` (everything when None), keyed
        by id, plus the max saved_at among them — the reader's next watermark.
        Strict `>` so a one-shot backfill that stamps every row with one timestamp
        doesn't get refetched in full every check; nothing is missed because a
        save_many commits atomically, so rows sharing a microsecond are all visible
        together and a read never lands between them. `omit_paths` matches `all()`."""
        data_col = self.table.c.data
        if omit_paths:
            data_col = strip_json_paths(data_col, self.engine.dialect.name, omit_paths)
        sa = self.table.c.saved_at
        def q(conn: Connection):
            stmt = select(self.pk, sa, data_col)
            if watermark is not None:
                stmt = stmt.where(sa > watermark)
            return conn.execute(stmt).all()
        rows = self._read(q)
        records = {r[0]: self.view(r[2]) for r in rows}
        high = max((r[1] for r in rows if r[1] is not None), default=None)
        return records, high


def stamp(rec: dict, section: str, payload: dict, token_field: str | None,
          token_value: str | None) -> None:
    """Set one section of `rec` in memory, stamping checked_at and (when a token
    field is given) the token the fact was computed against. Does not persist."""
    stamped = dict(payload)
    stamped["checked_at"] = now()
    if token_field is not None:
        stamped[token_field] = token_value
    rec[section] = stamped


def _age_days(checked_at: str, today: str | None) -> int | None:
    """How many days old `checked_at` is, measured against `today` (a plain ISO
    date) or the current UTC date. `checked_at` is stamped by `now` in UTC, so
    the reference date is UTC too: a local date would put the machine's timezone
    into the answer, and the age of a stored instant is a property of the
    instants alone."""
    if not checked_at:
        return None
    ref = date.fromisoformat(today) if today else datetime.now(timezone.utc).date()
    try:
        checked = datetime.fromisoformat(checked_at).date()
    except ValueError:
        return None
    return (ref - checked).days


def currency_failure_core(section: dict | None, token_field: str | None,
                          token_value: str | None, want_version: int | None = None,
                          max_age_days: int | None = None,
                          today: str | None = None) -> str | None:
    """Why `section` fails the currency check, as a short human phrase — None when
    current. Checks, in order: the section exists, matches the freshness token
    (when token_field is given), matches the wanted schema version (when given),
    and is within max_age_days (when given). `is_current_core` is this function's
    boolean projection, so a gate reason built from the phrase always agrees with
    the gate's verdict."""
    if not section:
        return "missing"
    if token_field is not None and section.get(token_field) != token_value:
        return "stale (computed against an earlier head)"
    if want_version is not None and section.get("schema_version") != want_version:
        return "produced under an old schema"
    if max_age_days is not None:
        age = _age_days(section.get("checked_at", ""), today)
        if age is None:
            return "unstamped (no checked_at date)"
        if age > max_age_days:
            return f"{age}d old, outside the {max_age_days}d window"
    return None


def is_current_core(section: dict | None, token_field: str | None, token_value: str | None,
                    want_version: int | None = None, max_age_days: int | None = None,
                    today: str | None = None) -> bool:
    """True iff `section` exists, matches the freshness token (when token_field is
    given), matches the wanted schema version (when given), and is within
    max_age_days (when given)."""
    return currency_failure_core(section, token_field, token_value,
                                 want_version, max_age_days, today) is None
