import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type TableSummary } from "../api";
import { formatCell, stringify } from "./tableCell";

const PREVIEW_COLS = 4;

export default function Tables() {
  const [tables, setTables] = useState<TableSummary[]>([]);
  const [err, setErr] = useState<string>();
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.tables().then((d) => setTables(d.tables)).catch((e) => setErr(String(e))).finally(() => setLoading(false));
  }, []);

  if (err) return <div className="error">Failed to load tables: {err}</div>;
  if (loading) return (
    <div className="explorer-loading">
      <span className="spinner explorer-loading-spinner" />
      <span className="explorer-loading-label">Loading tables…</span>
    </div>
  );

  return (
    <div className="tables-view">
      <div className="detail-head">
        <h1>🗄️ Tables</h1>
        <p className="muted">Every table in the triage store — row counts and a preview. Click a table to browse, sort, and filter its rows.</p>
      </div>
      <div className="tables-grid">
        {tables.map((t) => {
          const cols = t.columns.slice(0, PREVIEW_COLS);
          return (
            <div className="table-card" key={t.name}>
              <div className="table-card-head">
                <Link to={`/tables/${t.name}`} className="table-card-name">{t.name}</Link>
                <Link to={`/tables/${t.name}`} className="linkish table-card-count">{t.row_count.toLocaleString()} rows</Link>
              </div>
              {t.description && <p className="muted small table-card-desc">{t.description}</p>}
              {t.preview.length > 0 ? (
                <table className="grid compact table-card-preview">
                  <thead><tr>{cols.map((c) => <th key={c.name} className={c.json ? "col-json" : ""}>{c.name}</th>)}</tr></thead>
                  <tbody>
                    {t.preview.map((row, i) => (
                      <tr key={i}>
                        {cols.map((c) => (
                          <td key={c.name} className="muted small mono" title={stringify(row[c.name])}>{formatCell(row[c.name], 60)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : <p className="muted small">empty</p>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
