"""Read-only schema browser backing the app's Tables tab: row counts,
preview, JSON-blob expansion, pagination, sort, and filter."""
from __future__ import annotations

import pytest

from prospector_app.backend import tables


def _insert_prs(url: str, recs: list[dict]) -> None:
    """Insert PR records straight into the temp store, mirror columns and all."""
    from pipeline import schema, storekit
    eng = storekit.get_engine(url)
    with eng.begin() as conn:
        for rec in recs:
            conn.execute(schema.prs.insert().values(data=rec, **schema.mirror_pr(rec)))


def _pr(n: int, *, state: str = "open", disposition: str | None = None) -> dict:
    return {
        "pr": n,
        "meta": {"state": state, "title": f"PR {n}", "head_sha": f"sha{n}"},
        "analysis": {"disposition": disposition},
        "security": {"verdict": "GREEN"},
        "signals": {"greptile": 5},
    }


def test_overview_lists_every_table_with_counts(temp_store):
    _insert_prs(temp_store, [_pr(1), _pr(2)])
    summaries = tables.overview()
    names = {s["name"] for s in summaries}
    # every declared store table appears
    assert {"prs", "clusters", "issues", "activity", "runs"} <= names
    prs = next(s for s in summaries if s["name"] == "prs")
    assert prs["row_count"] == 2
    assert prs["description"]  # non-empty human description


def test_overview_expands_json_blob_into_dotted_columns(temp_store):
    _insert_prs(temp_store, [_pr(1, disposition="merge")])
    prs = next(s for s in tables.overview() if s["name"] == "prs")
    colnames = [c["name"] for c in prs["columns"]]
    # real columns first (no raw `data`), then dotted data.* virtual columns
    assert colnames[0] == "pr"
    assert "data" not in colnames
    assert "data.meta" in colnames and "data.analysis" in colnames
    # data.* columns are flagged json (display-only)
    assert next(c for c in prs["columns"] if c["name"] == "data.meta")["json"] is True
    assert next(c for c in prs["columns"] if c["name"] == "disposition")["json"] is False
    # the blob's top-level value is spliced in under its dotted key
    assert prs["preview"][0]["data.meta"] == {"state": "open", "title": "PR 1", "head_sha": "sha1"}
    assert prs["preview"][0]["disposition"] == "merge"


def test_rows_paginates_and_reports_total(temp_store):
    _insert_prs(temp_store, [_pr(n) for n in range(1, 6)])  # PRs 1..5
    page = tables.rows("prs", limit=2, offset=0, order="pr", dir="asc")
    assert page["total"] == 5
    assert [r["pr"] for r in page["rows"]] == [1, 2]
    page2 = tables.rows("prs", limit=2, offset=2, order="pr", dir="asc")
    assert [r["pr"] for r in page2["rows"]] == [3, 4]


def test_rows_sorts_desc(temp_store):
    _insert_prs(temp_store, [_pr(1), _pr(2), _pr(3)])
    page = tables.rows("prs", order="pr", dir="desc")
    assert [r["pr"] for r in page["rows"]] == [3, 2, 1]


def test_rows_filters_string_column_case_insensitively(temp_store):
    _insert_prs(temp_store, [_pr(1, disposition="merge"),
                             _pr(2, disposition="close-stale"),
                             _pr(3, disposition="merge")])
    page = tables.rows("prs", filters={"disposition": "MERGE"})
    assert {r["pr"] for r in page["rows"]} == {1, 3}
    assert page["total"] == 2


def test_rows_expands_blob_like_overview(temp_store):
    _insert_prs(temp_store, [_pr(1, disposition="merge")])
    page = tables.rows("prs")
    assert "data" not in {c["name"] for c in page["columns"]}
    assert page["rows"][0]["data.meta"]["title"] == "PR 1"


def test_unknown_table_raises(temp_store):
    with pytest.raises(tables.UnknownTable):
        tables.rows("nope")


def test_unknown_order_column_raises(temp_store):
    with pytest.raises(tables.UnknownColumn):
        tables.rows("prs", order="data.meta")  # virtual column — not sortable
    with pytest.raises(tables.UnknownColumn):
        tables.rows("prs", order="bogus")


def test_tables_routes_smoke(temp_store):
    from fastapi.testclient import TestClient
    from prospector_app.backend.app import app
    _insert_prs(temp_store, [_pr(1, disposition="merge"), _pr(2)])
    client = TestClient(app)

    r = client.get("/api/tables")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["tables"]}
    assert "prs" in names

    r = client.get("/api/tables/prs", params={"order": "pr", "dir": "desc"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["rows"][0]["pr"] == 2

    # f_<column> filter param → filters={"disposition": "merge"} (only PR #1 matches)
    r = client.get("/api/tables/prs", params={"f_disposition": "merge"})
    assert r.status_code == 200 and r.json()["total"] == 1
    # a filter on an unknown/virtual column is a 400 (filter-path column guard)
    assert client.get("/api/tables/prs", params={"f_bogus": "x"}).status_code == 400

    assert client.get("/api/tables/nope").status_code == 404
    assert client.get("/api/tables/prs", params={"order": "bogus"}).status_code == 400
