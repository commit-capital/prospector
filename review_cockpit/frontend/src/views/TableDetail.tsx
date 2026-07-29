import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { api, type TableColumn } from "../api";
import { formatCell, stringify } from "./tableCell";

const PAGE_SIZE = 50;
const FILTER_PREFIX = "f_";
const FILTER_DEBOUNCE_MS = 300;

export default function TableDetail() {
  const { name = "" } = useParams<{ name: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [columns, setColumns] = useState<TableColumn[]>([]);
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string>();

  const page = parseInt(searchParams.get("page") ?? "0", 10) || 0;
  const order = searchParams.get("order") ?? undefined;
  const dir = searchParams.get("dir") === "desc" ? "desc" : "asc";

  const filters = useMemo(() => {
    const out: Record<string, string> = {};
    searchParams.forEach((v, k) => { if (k.startsWith(FILTER_PREFIX) && v) out[k.slice(FILTER_PREFIX.length)] = v; });
    return out;
  }, [searchParams]);

  // Draft filter text (typed, pre-debounce), re-synced whenever the URL filters change.
  const [drafts, setDrafts] = useState<Record<string, string>>(filters);
  const syncedRef = useRef(filters);
  useEffect(() => {
    const prev = syncedRef.current;
    const pk = Object.keys(prev), nk = Object.keys(filters);
    const same = pk.length === nk.length && pk.every((k) => prev[k] === filters[k]);
    if (!same) { setDrafts(filters); syncedRef.current = filters; }
  }, [filters]);

  // Fetch the page whenever table / paging / sort / filters change.
  useEffect(() => {
    let aborted = false;
    setLoading(true); setErr(undefined); // eslint-disable-line react-hooks/set-state-in-effect -- kicks off the loading spinner for the table-page fetch
    const p = new URLSearchParams();
    p.set("limit", String(PAGE_SIZE));
    p.set("offset", String(page * PAGE_SIZE));
    if (order) { p.set("order", order); p.set("dir", dir); }
    Object.entries(filters).forEach(([col, v]) => p.set(`${FILTER_PREFIX}${col}`, v));
    api.tableRows(name, `?${p.toString()}`)
      .then((d) => { if (aborted) return; setColumns(d.columns); setRows(d.rows); setTotal(d.total); })
      .catch((e) => { if (!aborted) setErr(String(e)); })
      .finally(() => { if (!aborted) setLoading(false); });
    return () => { aborted = true; };
  }, [name, page, order, dir, filters]);

  // Debounce draft filter text into the URL search params.
  useEffect(() => {
    const timer = setTimeout(() => {
      const next = new URLSearchParams(searchParams);
      let changed = false;
      const draftKeys = new Set(Object.keys(drafts).filter((k) => drafts[k] !== ""));
      searchParams.forEach((v, k) => {
        if (!k.startsWith(FILTER_PREFIX)) return;
        const col = k.slice(FILTER_PREFIX.length);
        if (!draftKeys.has(col) || drafts[col] !== v) { next.delete(k); changed = true; }
      });
      Object.entries(drafts).forEach(([col, v]) => {
        if (v === "") return;
        const key = `${FILTER_PREFIX}${col}`;
        if (next.get(key) !== v) { next.set(key, v); changed = true; }
      });
      if (changed) { next.delete("page"); setSearchParams(next, { replace: true }); }
    }, FILTER_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [drafts, searchParams, setSearchParams]);

  const sortClick = (col: TableColumn) => {
    if (col.json) return; // data.* virtual columns are display-only
    const next = new URLSearchParams(searchParams);
    if (order !== col.name) { next.set("order", col.name); next.set("dir", "asc"); }
    else if (dir === "asc") { next.set("dir", "desc"); }
    else { next.delete("order"); next.delete("dir"); }
    next.delete("page");
    setSearchParams(next, { replace: true });
  };

  const goPage = (n: number) => {
    const next = new URLSearchParams(searchParams);
    if (n === 0) next.delete("page"); else next.set("page", String(n));
    setSearchParams(next, { replace: true });
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="table-detail">
      <div className="detail-head">
        <h1>🗄️ {name}</h1>
        <p className="muted">
          <Link to="/tables" className="linkish">← all tables</Link> · {total.toLocaleString()} rows
        </p>
      </div>
      {err && <div className="error">{err}</div>}
      {loading && rows.length === 0 ? (
        <div className="explorer-loading">
          <span className="spinner explorer-loading-spinner" />
          <span className="explorer-loading-label">Loading…</span>
        </div>
      ) : (
        <>
          <div className="table-scroll">
            <table className="grid compact">
              <thead>
                <tr>
                  {columns.map((c) => {
                    const arrow = order === c.name ? (dir === "asc" ? " ▲" : " ▼") : "";
                    return (
                      <th key={c.name} className={c.json ? "col-json" : "col-sortable"}
                        onClick={() => sortClick(c)}
                        title={c.json ? "expanded from the JSON data column — not sortable/filterable" : "click to sort"}>
                        {c.name}{arrow}
                      </th>
                    );
                  })}
                </tr>
                <tr>
                  {columns.map((c) => (
                    <th key={c.name}>
                      {!c.json && (
                        <input className="col-filter" type="text" value={drafts[c.name] ?? ""}
                          placeholder="filter…"
                          onChange={(e) => setDrafts((d) => ({ ...d, [c.name]: e.target.value }))}
                          onClick={(e) => e.stopPropagation()} />
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i}>
                    {columns.map((c) => (
                      <td key={c.name} className="muted small mono" title={stringify(row[c.name])}>
                        {formatCell(row[c.name], 80)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!loading && rows.length === 0 && <div className="callout">No matching rows.</div>}
          <div className="table-pager">
            <button className="btn-secondary sm" disabled={page === 0} onClick={() => goPage(page - 1)}>← prev</button>
            <span className="muted small">page {page + 1} of {totalPages}</span>
            <button className="btn-secondary sm" disabled={page >= totalPages - 1} onClick={() => goPage(page + 1)}>next →</button>
          </div>
        </>
      )}
    </div>
  );
}
