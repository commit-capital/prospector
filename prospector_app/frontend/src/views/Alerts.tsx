import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import { api, type AlertCaps, type AlertDetail, type AlertDismissResult, type AlertRow, type AlertSeverity, type AlertSource, type AlertVerdict } from "../api";
import { PRLink } from "../components/PRLink";
import { useExec } from "../ExecContext";
import { useRepoMeta } from "../RepoMetaContext";
import { stopRowOpen } from "../rowOpen";
import { cycleSort, type SortDir } from "../sortCycle";
import Advisories from "./Advisories";

const PAGE_SIZE = 50;
type AlertSortKey = "number" | "source" | "severity" | "title" | "state" | "verdict" | "links" | "updated";
const DESC_FIRST = new Set<AlertSortKey>(["number", "severity", "links", "updated"]);

const SOURCE_CHIP: Record<AlertSource, { label: string; cls: string; hint: string }> = {
  "code-scanning": { label: "code scan", cls: "chip-blue", hint: "GitHub code scanning (CodeQL/SARIF) — security and quality findings with file locations" },
  dependabot: { label: "dependabot", cls: "chip-purple", hint: "Dependabot — vulnerable dependency alerts" },
  "secret-scanning": { label: "secret", cls: "chip-red", hint: "GitHub secret scanning — credentials already committed to the repository (distinct from the per-PR threat scan, which checks incoming diffs)" },
};

const SEVERITY_CLS: Record<AlertSeverity, string> = {
  critical: "chip-red", high: "chip-red", medium: "chip-yellow", low: "chip-muted",
};

const VERDICT_CHIP: Record<AlertVerdict, { cls: string; hint: string }> = {
  fixed: { cls: "chip-green", hint: "The find-fixed pass tied a specific merged PR to the fix" },
  "likely-fixed": { cls: "chip-blue", hint: "The default branch no longer shows the flagged condition, but no single PR could be attributed" },
  "not-fixed": { cls: "chip-yellow", hint: "The flagged condition still appears present on the default branch" },
};

function SourceChip({ s }: { s: AlertSource }) {
  const { label, cls, hint } = SOURCE_CHIP[s];
  return <span className={`chip ${cls} sm`} title={hint}>{label}</span>;
}

function SeverityChip({ s }: { s: AlertSeverity }) {
  return <span className={`chip ${SEVERITY_CLS[s]} sm`} title="Normalized alert severity">{s}</span>;
}

function VerdictChip({ v }: { v: AlertVerdict | null }) {
  if (!v) return <span className="muted">—</span>;
  const { cls, hint } = VERDICT_CHIP[v];
  return <span className={`chip ${cls} sm`} title={hint}>{v}</span>;
}

function StateChip({ r }: { r: AlertRow }) {
  if (r.state === "open") return <span className="chip chip-yellow sm" title="Open on GitHub">open</span>;
  const hint = r.state === "fixed" ? "GitHub reports this alert fixed/revoked"
    : `Dismissed on GitHub${r.dismissed_reason ? ` (${r.dismissed_reason})` : ""}`;
  return <span className={`chip sm ${r.state === "fixed" ? "chip-green" : "chip-muted"}`} title={hint}>{r.state}</span>;
}

// Candidate PRs/issues found by the deterministic linker or the find-fixed
// agent. PRs open the in-app flyout when possible; issues link to GitHub.
function AlertLinks({ r }: { r: AlertRow }) {
  const { issueUrl } = useRepoMeta();
  if (!r.links.length) return <span className="muted">—</span>;
  return (
    <span className="issue-prs">
      {r.links.slice(0, 6).map((l, i) => (
        <span key={`${l.kind}-${l.number}`} title={`${l.how}${l.note ? ` — ${l.note}` : ""}`}>
          {i > 0 && " "}
          {l.kind === "pr"
            ? <PRLink n={l.number} className="pr-ref" />
            : <a href={issueUrl(l.number)} target="_blank" rel="noreferrer" className="gh-pr-link" onClick={stopRowOpen}>#{l.number}</a>}
          {(l.state === "merged" || l.state === "closed") && (
            <span className={`chip sm ${l.state === "merged" ? "chip-purple" : "chip-muted"}`}
                  title="Current state on GitHub">{l.state}</span>
          )}
        </span>
      ))}
      {r.links.length > 6 && <span className="muted small"> +{r.links.length - 6}</span>}
    </span>
  );
}

function identity(r: AlertRow): string {
  if (r.source === "dependabot") return r.package ? `${r.package}${r.ecosystem ? ` (${r.ecosystem})` : ""}` : r.title ?? "—";
  if (r.source === "secret-scanning") return r.title ?? r.secret_type ?? "—";
  return r.title ?? r.rule_id ?? "—";
}

function location(r: AlertRow): string {
  if (r.source === "dependabot") return r.manifest_path ?? "—";
  if (r.path) return r.start_line != null ? `${r.path}:${r.start_line}` : r.path;
  return "—";
}

function isAlertSource(value: string | null): value is AlertSource {
  return value !== null && value in SOURCE_CHIP;
}

// The dismissal form inside the detail panel: reason picker (per-source
// vocabulary from the backend), optional note, dry-run/live buttons. The gate
// (alert_gates.dismiss_eligibility) decides server-side; results render inline.
function DismissForm({ d, onDone }: { d: AlertDetail; onDone: () => void }) {
  const { dryRun, botLogin } = useExec();
  const [reason, setReason] = useState(d.dismiss_reasons[0] ?? "");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AlertDismissResult | null>(null);
  const run = async (asDryRun: boolean) => {
    setBusy(true);
    try {
      const res = await api.dismissAlert(d.source, d.number, reason, comment, asDryRun);
      setResult(res);
      if (res.status === "executed") onDone();
    } finally {
      setBusy(false);
    }
  };
  if (d.state !== "open") return null;
  return (
    <div className="detail-section">
      <h4>Dismiss upstream (as {botLogin})</h4>
      <div className="form-row">
        <select value={reason} onChange={(e) => setReason(e.target.value)} title="Dismissal reason — the exact vocabulary this alert source accepts">
          {d.dismiss_reasons.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
      </div>
      <textarea placeholder="Operator note (required for judgment reasons: false positive / won't fix / …)"
        value={comment} onChange={(e) => setComment(e.target.value)} rows={2} />
      <div className="form-row">
        <button className="btn-secondary sm" disabled={busy} onClick={() => run(true)}>Preview (dry-run)</button>
        <button className="btn-primary sm" disabled={busy || dryRun} title={dryRun ? "Live mode is off — toggle dry-run in the identity picker" : "Dismiss this alert on GitHub"}
          onClick={() => run(false)}>Dismiss</button>
      </div>
      {result && (
        <div className={`callout ${result.status === "executed" ? "ok" : result.status === "blocked" || result.status === "error" ? "err" : ""}`}>
          <b>{result.status}</b> — {result.detail}
        </div>
      )}
    </div>
  );
}

function DetailPanel({ source, number, onClose, onChanged }: {
  source: AlertSource; number: number; onClose: () => void; onChanged: () => void;
}) {
  const key = `${source}/${number}`;
  const [res, setRes] = useState<{ key: string; d?: AlertDetail; err?: string } | null>(null);
  useEffect(() => {
    let active = true;
    api.getAlert(source, number)
      .then((x) => { if (active) setRes({ key, d: x }); })
      .catch((e: Error) => { if (active) setRes({ key, err: e.message }); });
    return () => { active = false; };
  }, [source, number, key]);
  const current = res && res.key === key ? res : null;
  const d = current?.d ?? null;
  const err = current?.err;
  return (
    <aside className="detail-pane alert-detail">
      <div className="detail-head">
        <h3>{source}#{number}</h3>
        <button className="link-btn" onClick={onClose}>Close ✕</button>
      </div>
      {err && <div className="callout err">{err}</div>}
      {!d && !err && <div className="muted">Loading…</div>}
      {d && (
        <>
          <p>
            <SourceChip s={d.source} /> <SeverityChip s={d.severity} /> <StateChip r={d} />{" "}
            {d.quality && <span className="chip chip-muted sm" title="A code-quality finding (no security severity)">quality</span>}
          </p>
          <p><b>{identity(d)}</b></p>
          <p className="mono small">{location(d)}</p>
          <p><a href={d.html_url} target="_blank" rel="noreferrer" className="gh-pr-link">Open on GitHub ↗</a></p>
          {d.verdict && (
            <div className="detail-section">
              <h4>Find-fixed verdict</h4>
              <p><VerdictChip v={d.verdict} />{d.action && <span className="chip chip-muted sm" title="Suggested action">{d.action}</span>}</p>
              {d.evidence && <p className="small">{d.evidence}</p>}
            </div>
          )}
          {d.links.length > 0 && (
            <div className="detail-section">
              <h4>Linked PRs / issues</h4>
              <AlertLinks r={d} />
            </div>
          )}
          <DismissForm d={d} onDone={onChanged} />
        </>
      )}
    </aside>
  );
}

const SOURCE_FILTERS = [
  { key: "", label: "All sources" },
  { key: "code-scanning", label: "Code scan" },
  { key: "dependabot", label: "Dependabot" },
  { key: "secret-scanning", label: "Secrets" },
];
const STATE_FILTERS = [
  { key: "open", label: "Open" },
  { key: "dismissed", label: "Dismissed" },
  { key: "fixed", label: "Fixed" },
  { key: "", label: "All" },
];
const VERDICT_FILTERS = [
  { key: "", label: "Any verdict" },
  { key: "fixed", label: "Fixed" },
  { key: "likely-fixed", label: "Likely fixed" },
  { key: "not-fixed", label: "Not fixed" },
  { key: "none", label: "Unscanned" },
];

function AlertsTable() {
  const [params, setParams] = useSearchParams();
  const [caps, setCaps] = useState<AlertCaps | null>(null);
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [sortKey, setSortKey] = useState<AlertSortKey | "">("updated");
  const [sortDir, setSortDir] = useState<SortDir | "">("desc");
  const [sourceFilter, setSourceFilter] = useState("");
  const [stateFilter, setStateFilter] = useState("open");
  const [verdictFilter, setVerdictFilter] = useState("");
  const selectedSource = params.get("alert_source");
  const selectedNumber = params.get("alert");
  const selected = isAlertSource(selectedSource) && selectedNumber !== null && /^\d+$/.test(selectedNumber)
    ? { source: selectedSource, number: Number(selectedNumber) }
    : null;
  const [generation, setGeneration] = useState(0);
  // One result object per completed fetch, keyed by the query params it was
  // fetched for; "loading" is derived by comparing keys, so the effect never
  // sets state synchronously. Stale rows keep rendering while the next page
  // loads (matching the Issues table's feel).
  const queryKey = JSON.stringify([q, sortKey, sortDir, sourceFilter, stateFilter, verdictFilter, page, generation]);
  const [result, setResult] = useState<{ key: string; items: AlertRow[]; total: number; err?: string } | null>(null);

  useEffect(() => {
    api.alertCaps().then(setCaps).catch(() => setCaps(null));
  }, []);

  const reload = useCallback(() => setGeneration((g) => g + 1), []);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const run = () => {
      api.queryAlerts({
        q,
        sort: sortKey || undefined,
        direction: sortDir || undefined,
        source: sourceFilter || undefined,
        state: stateFilter || "all",
        verdict: verdictFilter || undefined,
        offset: (page - 1) * PAGE_SIZE,
        limit: PAGE_SIZE,
      }).then((res) => {
        if (!active) return;
        setResult({ key: queryKey, items: res.items, total: res.total });
        // While the backend's PR snapshot is still cold-loading, the link
        // chips lack current PR states — refetch quietly (same key, so the
        // rows stay put) until they arrive.
        if (res.pr_states_loading) timer = window.setTimeout(run, 2000);
      }).catch((e: Error) => {
        if (active) setResult({ key: queryKey, items: [], total: 0, err: e.message });
      });
    };
    run();
    return () => { active = false; if (timer !== undefined) window.clearTimeout(timer); };
  }, [q, sortKey, sortDir, sourceFilter, stateFilter, verdictFilter, page, generation, queryKey]);

  const rows = result?.items ?? [];
  const total = result?.total ?? 0;
  const loading = !result || result.key !== queryKey;
  const err = result && result.key === queryKey ? result.err : undefined;

  const clickSort = (key: AlertSortKey) => {
    const next = cycleSort({ key: sortKey, dir: sortDir }, key, DESC_FIRST);
    setSortKey(next.key);
    setSortDir(next.dir);
    setPage(1);
  };
  const indicator = (key: AlertSortKey) => sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "";
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const start = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const end = Math.min(page * PAGE_SIZE, total);

  const unavailable = caps !== null && !(["code-scanning", "dependabot", "secret-scanning"] as AlertSource[]).some((s) => caps.sources[s]);
  const selectAlert = (source: AlertSource, number: number): void => {
    const next = new URLSearchParams(params);
    next.set("security", "alerts");
    next.set("alert_source", source);
    next.set("alert", String(number));
    next.delete("advisory");
    setParams(next);
  };
  const closeAlert = (): void => {
    const next = new URLSearchParams(params);
    next.delete("alert_source");
    next.delete("alert");
    setParams(next);
  };

  return (
    <>
      <p className="muted small">
        GitHub repository security &amp; quality alerts — code scanning, Dependabot, and
        GitHub secret scanning (secrets already in the repo; the per-PR threat scan of
        incoming diffs is separate). Read and actioned as the configured bot App.
      </p>
      {unavailable && (
        <div className="callout">
          No alert source is readable. Either the bot token can't be minted on this
          machine, or the GitHub App hasn't been granted the repository permissions
          (Code scanning alerts, Dependabot alerts, Secret scanning alerts — read/write),
          or the features are disabled on the repository. Run the alert ingest from the
          Control tab once access is granted.
        </div>
      )}
      {caps !== null && (
        <p className="muted small">
          Sources: {(["code-scanning", "dependabot", "secret-scanning"] as AlertSource[]).map((s) => (
            <span key={s}>{SOURCE_CHIP[s].label}: {caps.sources[s] ? "✓" : "✗"}{"  "}</span>
          ))}
        </p>
      )}
      {err && <div className="callout err">{err}</div>}
      <div className="table-toolbar">
        <input className="search" placeholder="Search alerts…" value={q}
          onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        <div className="segmented" title="Filter by alert source">
          {SOURCE_FILTERS.map((f) => (
            <button key={f.key} className={sourceFilter === f.key ? "on" : ""}
              onClick={() => { setSourceFilter(f.key); setPage(1); }}>{f.label}</button>
          ))}
        </div>
        <div className="segmented" title="Filter by normalized alert state">
          {STATE_FILTERS.map((f) => (
            <button key={f.key} className={stateFilter === f.key ? "on" : ""}
              onClick={() => { setStateFilter(f.key); setPage(1); }}>{f.label}</button>
          ))}
        </div>
        <div className="segmented" title="Filter by the find-fixed pass's verdict">
          {VERDICT_FILTERS.map((f) => (
            <button key={f.key} className={verdictFilter === f.key ? "on" : ""}
              onClick={() => { setVerdictFilter(f.key); setPage(1); }}>{f.label}</button>
          ))}
        </div>
        <span className="muted small">
          {loading ? "Loading…" : total === 0 ? "No alerts" : `${start}-${end} of ${total}`}
        </span>
        <button className="btn-secondary sm" disabled={page <= 1 || loading} onClick={() => setPage(page - 1)}>Prev</button>
        <button className="btn-secondary sm" disabled={page >= pages || loading} onClick={() => setPage(page + 1)}>Next</button>
      </div>
      {loading && !result && (
        <div className="explorer-loading">
          <span className="spinner explorer-loading-spinner" />
          <span className="explorer-loading-label">Loading alerts…</span>
        </div>
      )}
      {result && (
      <div className="alerts-layout">
        <div className="table-wrap">
        <table className="grid sortable alerts-table">
          <thead><tr>
            <th onClick={() => clickSort("number")} title="Per-source alert number">#{indicator("number")}</th>
            <th onClick={() => clickSort("source")}>Source{indicator("source")}</th>
            <th onClick={() => clickSort("severity")}>Severity{indicator("severity")}</th>
            <th onClick={() => clickSort("title")}>Alert{indicator("title")}</th>
            <th>Location</th>
            <th onClick={() => clickSort("state")}>State{indicator("state")}</th>
            <th onClick={() => clickSort("verdict")} title="The find-fixed pass's verdict">Fix scan{indicator("verdict")}</th>
            <th onClick={() => clickSort("links")}>Links{indicator("links")}</th>
            <th onClick={() => clickSort("updated")}>Updated{indicator("updated")}</th>
          </tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className={`rowlink ${selected && selected.source === r.source && selected.number === r.number ? "row-selected" : ""}`}
                  onClick={() => selectAlert(r.source, r.number)}>
                <td className="mono">
                  <a href={r.html_url} target="_blank" rel="noreferrer" className="gh-pr-link"
                     title="Open this alert on GitHub ↗" onClick={stopRowOpen}>#{r.number}</a>
                </td>
                <td><SourceChip s={r.source} /></td>
                <td><SeverityChip s={r.severity} />{r.quality && <span className="chip chip-muted sm" title="A code-quality finding (no security severity)">q</span>}</td>
                <td>{identity(r)}</td>
                <td className="mono small">{location(r)}</td>
                <td><StateChip r={r} /></td>
                <td><VerdictChip v={r.verdict} /></td>
                <td onClick={stopRowOpen}><AlertLinks r={r} /></td>
                <td className="muted small">{(r.updated_at ?? "").slice(0, 10) || "—"}</td>
              </tr>
            ))}
            {!loading && rows.length === 0 && (
              <tr><td colSpan={9} className="muted">No matching alerts. Run the alert ingest from the Control tab to fetch them.</td></tr>
            )}
          </tbody>
        </table>
        </div>
        {selected && (
          <DetailPanel source={selected.source} number={selected.number}
            onClose={closeAlert} onChanged={reload} />
        )}
      </div>
      )}
    </>
  );
}

type Section = "advisories" | "alerts";

export default function Alerts() {
  const [params, setParams] = useSearchParams();
  const section: Section = params.has("advisory")
    ? "advisories"
    : params.has("alert") || params.get("security") === "alerts" ? "alerts" : "advisories";
  const selectSection = (nextSection: Section): void => {
    const next = new URLSearchParams(params);
    next.set("security", nextSection);
    next.delete("advisory");
    next.delete("alert_source");
    next.delete("alert");
    setParams(next);
  };
  return (
    <div className="view alerts-view">
      <h2>🛡️ Alerts</h2>
      <div className="segmented" title="Advisories are privately reported vulnerabilities; alerts are automated scanner findings">
        <button className={section === "advisories" ? "on" : ""} onClick={() => selectSection("advisories")}>Advisories</button>
        <button className={section === "alerts" ? "on" : ""} onClick={() => selectSection("alerts")}>Alerts</button>
      </div>
      {section === "advisories" ? <Advisories /> : <AlertsTable />}
    </div>
  );
}
