"""Physical schema for the SQL backing store — SQLAlchemy table definitions plus
the per-table mirror functions that project a record's hot fields into indexed
columns. The full record always lives in the portable `data` JSON column (JSONB
on Postgres, JSON/TEXT on SQLite); the mirror columns exist only so the store can
be queried without parsing JSON. Keep mirror columns in sync with the record:
each is read from the record on every save."""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Integer, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB

METADATA = MetaData()

# JSON on SQLite, JSONB on Postgres — one declaration, portable.
_JSON = JSON().with_variant(JSONB, "postgresql")

# Version of the store's record shapes, shared by every table in the database
# (PR and issue families alike). Stamped into the `registries` row named
# "schema" on the store's first write; storekit's write guard compares it to
# this constant so a checkout behind the store's stamp refuses to write. Bump
# it in any PR that changes record shape in a way older code mishandles.
# 3 — verify_base carries the captured regress baseline.
# 4 — verify_request gains the waiting-for-base status.
# 5 — issues gain the close-fixed disposition and the fix_scan section.
# 6 — verify gains the agent-verified outcome and the authored_test signal.
# 7 — analysis.disposition holds ANALYZE's verdict verbatim; a stored `merge`
#     may coexist with a blocking security/verify fact (the route derives on
#     read), which older validators refuse to save.
# 8 — the verify_worker registry keys per-host heartbeat records under `hosts`
#     (older writers clobber the map with a flat single-host record);
#     verify_request records the claiming `host`.
# 9 — a transiently failed verify_request re-queues with an `attempts` count
#     and may carry the fetch-error error_kind (older validators refuse to
#     save a record carrying that kind).
# 10 — PRs carry a fix_request section (the autofix queue), which older
#     validators drop as an unknown section rather than round-tripping.
# 11 — the alerts table (GitHub code-scanning / dependabot / secret-scanning
#     alert family) plus the "alert" runs-ledger kind.
STORE_SCHEMA_VERSION = 11

# saved_at is a microsecond-resolution ISO timestamp stamped on every save — when
# the store row was last written (distinct from `updated_at`, which mirrors the
# upstream entity's own timestamp). A cached reader keeps the max saved_at it has
# seen and refetches only rows with `saved_at > watermark`, so the database does
# the change-filtering. ISO text compares chronologically, so the filter is a
# plain string comparison.
prs = Table(
    "prs", METADATA,
    Column("pr", Integer, primary_key=True),
    Column("data", _JSON, nullable=False),
    Column("disposition", String, index=True),
    Column("state", String, index=True),
    Column("security_verdict", String, index=True),
    Column("head_sha", String),
    Column("updated_at", String),
    Column("saved_at", String, index=True),
)

clusters = Table(
    "clusters", METADATA,
    Column("id", Integer, primary_key=True),
    Column("data", _JSON, nullable=False),
    Column("outcome", String, index=True),
    Column("deleted", Boolean, index=True),  # soft-delete tombstone; reaped out of band
    Column("updated_at", String),
    Column("saved_at", String, index=True),
)

issues = Table(
    "issues", METADATA,
    Column("issue", Integer, primary_key=True),
    Column("data", _JSON, nullable=False),
    Column("disposition", String, index=True),
    Column("state", String, index=True),
    Column("updated_at", String),
    Column("saved_at", String, index=True),
)

issue_clusters = Table(
    "issue_clusters", METADATA,
    Column("id", Integer, primary_key=True),
    Column("data", _JSON, nullable=False),
    Column("canonical", Integer),
    Column("updated_at", String),
    Column("saved_at", String, index=True),
)

# GitHub repository security alerts (code scanning / Dependabot / secret
# scanning), one row per alert. `id` is the stable synthetic key
# alert_store.alert_id(source, number); the per-source alert number rides in
# the `number` mirror column.
alerts = Table(
    "alerts", METADATA,
    Column("id", Integer, primary_key=True),
    Column("data", _JSON, nullable=False),
    Column("source", String, index=True),
    Column("number", Integer, index=True),
    Column("state", String, index=True),
    Column("severity", String, index=True),
    Column("updated_at", String),
    Column("saved_at", String, index=True),
)

activity = Table(
    "activity", METADATA,
    Column("rowid", Integer, primary_key=True, autoincrement=True),
    Column("at", String, index=True),
    Column("kind", String, index=True),
    Column("operator", String, index=True),
    Column("pr", Integer, index=True),
    Column("dry_run", Boolean, index=True),
    Column("data", _JSON, nullable=False),
)

chat_messages = Table(
    "chat_messages", METADATA,
    Column("rowid", Integer, primary_key=True, autoincrement=True),
    Column("operator", String, index=True),
    Column("ctx_id", String, index=True),
    Column("role", String),
    Column("text", String),
    Column("at", String),
)

runs = Table(
    "runs", METADATA,
    Column("rowid", Integer, primary_key=True, autoincrement=True),
    Column("kind", String, index=True),  # "pr", "issue", or "alert" ledger
    Column("data", _JSON, nullable=False),
    Column("ts", String, index=True),
)

registries = Table(
    "registries", METADATA,
    # "threats" | "action_items" | "response_acks" | "schema" | "repo"
    #   | "schema_fingerprint"
    Column("name", String, primary_key=True),
    Column("data", _JSON, nullable=False),
)

# Shared cache of fetched PR diffs, one row per PR head. A head's diff never
# changes, so rows are immutable once written; `body` is the same
# MAX_DIFF_BYTES-capped text the machine-local file cache holds (diff_cache.py
# is the writer and owns the cap). `pr` labels the row for inspection and may
# be NULL when the head no longer maps to a known PR.
diffs = Table(
    "diffs", METADATA,
    Column("head_sha", String, primary_key=True),
    Column("pr", Integer, index=True),
    Column("body", String, nullable=False),
    Column("fetched_at", String),
)

agent_memory = Table(
    "agent_memory", METADATA,
    Column("rowid", Integer, primary_key=True, autoincrement=True),
    Column("id", String, unique=True),
    Column("at", String, index=True),
    Column("author", String),
    Column("text", String),
    Column("why", String),
    Column("tags", _JSON),
)

training_decisions = Table(
    "training_decisions", METADATA,
    Column("rowid", Integer, primary_key=True, autoincrement=True),
    Column("at", String, index=True),
    Column("pr", Integer, index=True),
    Column("decision", String, index=True),
    Column("by", String, index=True),
    Column("dry_run", Boolean, index=True),
    Column("data", _JSON, nullable=False),
)


def activity_row(event: dict) -> dict:
    """Project an activity event into the activity table's hot columns + full
    `data`. `pr` is coerced to int (or None); `dry_run` to bool."""
    pr = event.get("pr")
    pr = pr if isinstance(pr, int) else (int(pr) if isinstance(pr, str) and pr.isdigit() else None)
    return {
        "at": event.get("at"),
        "kind": event.get("kind"),
        "operator": event.get("operator"),
        "pr": pr,
        "dry_run": bool(event.get("dry_run")),
        "data": event,
    }


def training_decision_row(rec: dict) -> dict:
    """Project a captured human decision into the training_decisions table's hot
    columns + full `data`. `pr` is coerced to int (or None); `dry_run` to bool."""
    pr = rec.get("pr")
    pr = pr if isinstance(pr, int) else (int(pr) if isinstance(pr, str) and pr.isdigit() else None)
    return {
        "at": rec.get("at"),
        "pr": pr,
        "decision": rec.get("decision"),
        "by": rec.get("by"),
        "dry_run": bool(rec.get("dry_run")),
        "data": rec,
    }


def mirror_pr(rec: dict) -> dict:
    meta = rec.get("meta") or {}
    analysis = rec.get("analysis") or {}
    security = rec.get("security") or {}
    return {
        "pr": rec["pr"],
        "disposition": analysis.get("disposition"),
        "state": meta.get("state"),
        "security_verdict": security.get("verdict"),
        "head_sha": meta.get("head_sha"),
        "updated_at": meta.get("updated_at"),
    }


def mirror_cluster(rec: dict) -> dict:
    return {
        "id": rec["id"],
        "outcome": rec.get("outcome"),
        "deleted": bool(rec.get("deleted")),
        "updated_at": rec.get("checked_at"),
    }


def mirror_issue(rec: dict) -> dict:
    meta = rec.get("meta") or {}
    analysis = rec.get("analysis") or {}
    return {
        "issue": rec["issue"],
        "disposition": analysis.get("disposition"),
        "state": meta.get("state"),
        "updated_at": meta.get("updated_at"),
    }


def mirror_issue_cluster(rec: dict) -> dict:
    return {
        "id": rec["id"],
        "canonical": rec.get("canonical"),
        "updated_at": rec.get("checked_at"),
    }


def mirror_alert(rec: dict) -> dict:
    meta = rec.get("meta") or {}
    return {
        "id": rec["id"],
        "source": meta.get("source"),
        "number": meta.get("number"),
        "state": meta.get("state"),
        "severity": meta.get("severity"),
        "updated_at": meta.get("updated_at"),
    }
