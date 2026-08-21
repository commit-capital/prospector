"""Read-only schema browser backing the app's Tables tab.

Introspects the store's SQL tables (`schema.METADATA`) and serves, per table, a
row count + preview for the overview grid and a paginated / sortable / filterable
page for the detail view. The store keeps most content in a JSON `data` column;
each row's blob is expanded one level into `data.<key>` virtual columns so the
browser reads tabularly. Sorting and filtering apply to real SQL columns only —
the `data.*` virtual columns are display-only.
"""
from __future__ import annotations

from typing import Any, TypedDict

from sqlalchemy import Boolean, Column, Integer, String, Table, func, select
from sqlalchemy.sql import ColumnElement

from pipeline import schema
from pipeline import store
from pipeline import storekit

JsonValue = Any  # a JSON-serializable cell value

# One human sentence per store table. The *set* of tables comes from
# schema.METADATA (authoritative); a table missing here renders an empty
# description, so this map is best-effort documentation, not a gate.
DESCRIPTIONS: dict[str, str] = {
    "prs": "One row per open PR — indexed mirror columns plus the full triage "
           "record in `data` (meta / signals / drift / summary / cluster / "
           "analysis / security / issues / threat).",
    "clusters": "Semantic PR clusters with stable IDs; `data` holds members, "
                "per-member proposals, and the summary. `deleted` is a "
                "soft-delete tombstone.",
    "issues": "One row per triaged GitHub issue — mirror columns plus the issue "
              "record in `data`.",
    "issue_clusters": "Issue clusters; `canonical` names the representative "
                      "issue and `data` carries the members.",
    "activity": "Append-only audit trail of Prospector actions — merges, closes, "
                "reopens, handoffs (live and dry-run).",
    "chat_messages": "Persisted Prospector chat-agent transcript, one row per message.",
    "runs": "Ledger of pipeline phase runs, one row per run (`kind` = pr or issue).",
    "registries": "Singleton registries — the durable threat blocklist / incident "
                  "log and the action-items list — keyed by name.",
    "agent_memory": "The Prospector agent's durable memories: one fact per row with "
                    "author, text, and tags.",
    "diffs": "Shared PR-diff cache — one immutable row per fetched PR head, "
             "holding the capped diff text every machine's local file cache "
             "reads through.",
}

PREVIEW_ROWS = 5


class UnknownTable(Exception):
    """The requested table isn't part of the store schema."""


class UnknownColumn(Exception):
    """An order/filter names a column the table doesn't have (or the JSON blob)."""


class TableColumn(TypedDict):
    name: str   # a real column ("disposition") or a dotted virtual one ("data.analysis")
    json: bool  # True for an expanded data.* virtual column (display-only)


class TableSummary(TypedDict):
    name: str
    description: str
    row_count: int
    columns: list[TableColumn]
    preview: list[dict[str, JsonValue]]


class TablePage(TypedDict):
    name: str
    columns: list[TableColumn]
    rows: list[dict[str, JsonValue]]
    total: int


def _engine() -> storekit.Engine:
    """The shared store engine (same DB as the PR/issue store), re-resolved each
    call so a test that repoints settings.store_url() is honored; storekit caches
    one engine per URL."""
    return storekit.get_engine(storekit.resolve_url(None, store.DEFAULT_ROOT))


def _table(name: str) -> Table:
    tbl = schema.METADATA.tables.get(name)
    if tbl is None:
        raise UnknownTable(name)
    return tbl


def _expand(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Splice the JSON `data` blob's top-level keys in as `data.<key>` entries
    and drop the raw `data` column; every other column passes through unchanged."""
    out: dict[str, JsonValue] = {}
    for col, val in row.items():
        if col == "data" and isinstance(val, dict):
            for k, v in val.items():
                out[f"data.{k}"] = v
        else:
            out[col] = val
    return out


def _columns(tbl: Table, rows: list[dict[str, JsonValue]]) -> list[TableColumn]:
    """The display column list: real SQL columns (minus raw `data`) first, then
    the dotted data.* virtual columns discovered across `rows`, in first-seen
    order."""
    cols: list[TableColumn] = [
        {"name": c.name, "json": False} for c in tbl.columns if c.name != "data"
    ]
    if "data" in tbl.columns:
        seen: set[str] = set()
        for row in rows:
            blob = row.get("data")
            if isinstance(blob, dict):
                for k in blob:
                    key = f"data.{k}"
                    if key not in seen:
                        seen.add(key)
                        cols.append({"name": key, "json": True})
    return cols


def overview() -> list[TableSummary]:
    """Every store table (in schema-declaration order) with its row count, its
    column list, and a short preview — the data for the Tables grid."""
    eng = _engine()
    out: list[TableSummary] = []
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for name, tbl in schema.METADATA.tables.items():
            count = conn.execute(select(func.count()).select_from(tbl)).scalar() or 0
            raw = conn.execute(select(tbl).limit(PREVIEW_ROWS)).mappings().all()
            rows = [dict(r) for r in raw]
            out.append({
                "name": name,
                "description": DESCRIPTIONS.get(name, ""),
                "row_count": int(count),
                "columns": _columns(tbl, rows),
                "preview": [_expand(r) for r in rows],
            })
    return out


def _real_column(tbl: Table, name: str) -> Column[Any]:
    """The real SQL column `name` on `tbl`. The raw `data` blob and any dotted
    virtual column are not real columns, so they raise UnknownColumn."""
    if name == "data" or name not in tbl.columns:
        raise UnknownColumn(name)
    return tbl.columns[name]


def _filter_cond(col: Column[Any], raw: str) -> ColumnElement[bool] | None:
    """A WHERE condition for one filter value, or None when it doesn't apply:
    substring-ilike for text, equality for int/bool, nothing for other types."""
    if raw == "":
        return None
    kind = col.type
    if isinstance(kind, String):
        return col.ilike(f"%{raw}%")
    if isinstance(kind, Boolean):
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return col.is_(True)
        if low in ("false", "0", "no"):
            return col.is_(False)
        return None
    if isinstance(kind, Integer):
        try:
            return col == int(raw)
        except ValueError:
            return None
    return None


def _where(tbl: Table, filters: dict[str, str]) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = []
    for name, raw in filters.items():
        cond = _filter_cond(_real_column(tbl, name), raw)
        if cond is not None:
            conds.append(cond)
    return conds


def rows(name: str, *, limit: int = 50, offset: int = 0,
         order: str | None = None, dir: str = "asc",
         filters: dict[str, str] | None = None) -> TablePage:
    """One table's rows for the detail view: a filtered, ordered page plus the
    matching total. `order` and every filter key must name a real SQL column
    (else UnknownColumn); the raw `data` blob is expanded like overview()."""
    tbl = _table(name)
    conds = _where(tbl, filters or {})
    page_stmt = select(tbl)
    count_stmt = select(func.count()).select_from(tbl)
    for cond in conds:
        page_stmt = page_stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    if order:
        col = _real_column(tbl, order)
        page_stmt = page_stmt.order_by(col.desc() if dir == "desc" else col.asc())
    else:
        page_stmt = page_stmt.order_by(*tbl.primary_key.columns)
    page_stmt = page_stmt.limit(limit).offset(offset)
    eng = _engine()
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        total = conn.execute(count_stmt).scalar() or 0
        raw = conn.execute(page_stmt).mappings().all()
    page = [dict(r) for r in raw]
    return {
        "name": name,
        "columns": _columns(tbl, page),
        "rows": [_expand(r) for r in page],
        "total": int(total),
    }
