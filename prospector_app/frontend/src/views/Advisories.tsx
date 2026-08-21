import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type AdvisoryDetail, type AdvisoryRow, type AdvisorySeverity, type AdvisoryState, type AdvisoryVerdict, type AlertCaps } from "../api";
import { PRLink } from "../components/PRLink";
import { useRepoMeta } from "../RepoMetaContext";
import { stopRowOpen } from "../rowOpen";
import { cycleSort, type SortDir } from "../sortCycle";
import { timeAgo } from "../timeAgo";

const PAGE_SIZE = 50;
type SortKey = "ghsa" | "state" | "severity" | "summary" | "reporter" | "verdict" | "links" | "created" | "updated";
const DESC_FIRST = new Set<SortKey>(["severity", "links", "created", "updated"]);

const STATE_CHIP: Record<AdvisoryState, { cls: string; hint: string }> = {
  triage: { cls: "chip-yellow", hint: "Privately reported; no maintainer has accepted or closed it" },
  draft: { cls: "chip-blue", hint: "Accepted by a maintainer; being worked in private" },
  published: { cls: "chip-green", hint: "Public; downstream users have been notified" },
  closed: { cls: "chip-muted", hint: "Closed without publishing (duplicate, not a vulnerability, out of scope)" },
  withdrawn: { cls: "chip-muted", hint: "Published, then withdrawn" },
};
const SEVERITY_CLS: Record<AdvisorySeverity, string> = {
  critical: "chip-red", high: "chip-red", medium: "chip-yellow", low: "chip-muted", unknown: "chip-muted",
};
const VERDICT_CHIP: Record<AdvisoryVerdict, { cls: string; hint: string }> = {
  fixed: { cls: "chip-green", hint: "A specific default-branch commit removes the described behavior" },
  "likely-fixed": { cls: "chip-blue", hint: "The described code path is gone or guarded, but no single commit could be attributed" },
  "not-fixed": { cls: "chip-yellow", hint: "The described behavior still appears present on the default branch" },
  duplicate: { cls: "chip-purple", hint: "Same root cause as another advisory" },
};

function StateChip({ s }: { s: AdvisoryState }) {
  const { cls, hint } = STATE_CHIP[s];
  return <span className={`chip ${cls} sm`} title={hint}>{s}</span>;
}
function SeverityChip({ s }: { s: AdvisorySeverity }) {
  return <span className={`chip ${SEVERITY_CLS[s]} sm`} title="Reporter-assigned severity">{s}</span>;
}
function VerdictChip({ r }: { r: AdvisoryRow }) {
  if (!r.verdict) return <span className="muted">—</span>;
  const { cls, hint } = VERDICT_CHIP[r.verdict];
  return (
    <span className={`chip ${cls} sm`} title={hint}>
      {r.verdict}{r.verdict === "duplicate" && r.duplicate_of ? ` → ${r.duplicate_of}` : ""}
    </span>
  );
}

function Links({ r }: { r: AdvisoryRow }) {
  const { issueUrl } = useRepoMeta();
  if (!r.links.length) return <span className="muted">—</span>;
  return (
    <span className="issue-prs">
      {r.links.slice(0, 6).map((l, i) => (
        <span key={`${l.kind}-${l.number}`} title={l.how}>
          {i > 0 && " "}
          {l.kind === "pr"
            ? <PRLink n={l.number} className="pr-ref" />
            : <a href={issueUrl(l.number)} target="_blank" rel="noreferrer" className="gh-pr-link" onClick={stopRowOpen}>#{l.number}</a>}
          {(l.state === "merged" || l.state === "closed") && (
            <span className={`chip sm ${l.state === "merged" ? "chip-purple" : "chip-muted"}`} title="Current state on GitHub">{l.state}</span>
          )}
        </span>
      ))}
      {r.links.length > 6 && <span className="muted small"> +{r.links.length - 6}</span>}
    </span>
  );
}

function DetailPanel({ ghsa, onClose }: { ghsa: string; onClose: () => void }) {
  const { meta } = useRepoMeta();
  const [res, setRes] = useState<{ key: string; d?: AdvisoryDetail; err?: string } | null>(null);
  useEffect(() => {
    let active = true;
    api.getAdvisory(ghsa)
      .then((x) => { if (active) setRes({ key: ghsa, d: x }); })
      .catch((e: Error) => { if (active) setRes({ key: ghsa, err: e.message }); });
    return () => { active = false; };
  }, [ghsa]);
  const current = res && res.key === ghsa ? res : null;
  const d = current?.d ?? null;
  return (
    <aside className="detail-pane alert-detail">
      <div className="detail-head">
        <h3 className="mono">{ghsa}</h3>
        <button className="link-btn" onClick={onClose}>Close ✕</button>
      </div>
      {current?.err && <div className="callout err">{current.err}</div>}
      {!d && !current?.err && <div className="muted">Loading…</div>}
      {d && (
        <>
          <p><StateChip s={d.state} /> <SeverityChip s={d.severity} />{d.cve_id && <span className="chip chip-muted sm mono">{d.cve_id}</span>}</p>
          <p><b>{d.summary}</b></p>
          <p className="muted small">Reported by {d.reporter ?? "—"} · {(d.created_at ?? "").slice(0, 10)}{d.cwe_ids.length > 0 && ` · ${d.cwe_ids.join(", ")}`}</p>
          <p><a href={d.html_url} target="_blank" rel="noreferrer" className="gh-pr-link">Open on GitHub ↗</a></p>
          {d.verdict && (
            <div className="detail-section">
              <h4>Find-fixed verdict <span className="muted small">({d.by})</span></h4>
              <p><VerdictChip r={d} />
                {d.fix_commit && (
                  <> {meta
                    ? <a className="mono small" href={`${meta.url}/commit/${d.fix_commit}`} target="_blank" rel="noreferrer">{d.fix_commit.slice(0, 12)}</a>
                    : <span className="mono small">{d.fix_commit.slice(0, 12)}</span>}</>
                )}
              </p>
              {d.evidence && <p className="small">{d.evidence}</p>}
            </div>
          )}
          {d.links.length > 0 && (
            <div className="detail-section"><h4>Linked PRs</h4><Links r={d} /></div>
          )}
          <div className="detail-section">
            <h4>Report</h4>
            <div className="markdown small">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{d.description.slice(0, 20000)}</ReactMarkdown>
              {d.description.length > 20000 && <p className="muted small">Report truncated at 20,000 characters — open on GitHub for the full text.</p>}
            </div>
          </div>
        </>
      )}
    </aside>
  );
}

const STATE_FILTERS: { key: string; label: string; states: string[] }[] = [
  { key: "open", label: "Triage + draft", states: ["triage", "draft"] },
  { key: "triage", label: "Triage", states: ["triage"] },
  { key: "published", label: "Published", states: ["published"] },
  { key: "withdrawn", label: "Withdrawn", states: ["withdrawn"] },
  { key: "", label: "All", states: [] },
];
const VERDICT_FILTERS = [
  { key: "", label: "Any verdict" },
  { key: "fixed", label: "Fixed" },
  { key: "likely-fixed", label: "Likely fixed" },
  { key: "duplicate", label: "Duplicate" },
  { key: "not-fixed", label: "Not fixed" },
  { key: "none", label: "Unscanned" },
];

export default function Advisories() {
  const [caps, setCaps] = useState<AlertCaps | null>(null);
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [sortKey, setSortKey] = useState<SortKey | "">("severity");
  const [sortDir, setSortDir] = useState<SortDir | "">("desc");
  const [stateFilter, setStateFilter] = useState("open");
  const [verdictFilter, setVerdictFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const queryKey = JSON.stringify([q, sortKey, sortDir, stateFilter, verdictFilter, page]);
  const [result, setResult] = useState<{ key: string; items: AdvisoryRow[]; total: number; err?: string } | null>(null);

  useEffect(() => {
    api.alertCaps().then(setCaps).catch(() => setCaps(null));
  }, []);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const states = STATE_FILTERS.find((f) => f.key === stateFilter)?.states ?? [];
    const run = () => {
      api.queryAdvisories({
        q, sort: sortKey || undefined, direction: sortDir || undefined,
        state: states.length ? states : "all", verdict: verdictFilter || undefined,
        offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE,
      }).then((res) => {
        if (!active) return;
        setResult({ key: queryKey, items: res.items, total: res.total });
        if (res.pr_states_loading) timer = window.setTimeout(run, 2000);
      }).catch((e: Error) => {
        if (active) setResult({ key: queryKey, items: [], total: 0, err: e.message });
      });
    };
    run();
    return () => { active = false; if (timer !== undefined) window.clearTimeout(timer); };
  }, [q, sortKey, sortDir, stateFilter, verdictFilter, page, queryKey]);

  const rows = result?.items ?? [];
  const total = result?.total ?? 0;
  const loading = !result || result.key !== queryKey;
  const err = result && result.key === queryKey ? result.err : undefined;
  const clickSort = (key: SortKey) => {
    const next = cycleSort({ key: sortKey, dir: sortDir }, key, DESC_FIRST);
    setSortKey(next.key); setSortDir(next.dir); setPage(1);
  };
  const indicator = (key: SortKey) => sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "";
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const start = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const end = Math.min(page * PAGE_SIZE, total);
  const unavailable = caps !== null && !caps.sources.advisory;

  return (
    <>
      <p className="muted small">
        Privately reported repository security advisories, read as the configured bot App. The
        find-fixed pass marks each open report fixed, likely fixed, duplicate, or not fixed; acting
        on a report (accept, close, publish) happens on GitHub.
      </p>
      {unavailable && (
        <div className="callout">
          Advisories aren't readable. Either the bot token can't be minted on this machine or the
          GitHub App lacks the Repository security advisories (read) permission. Run the security
          sweep from the Control tab once access is granted.
        </div>
      )}
      {err && <div className="callout err">{err}</div>}
      <div className="table-toolbar">
        <input className="search" placeholder="Search advisories (GHSA, summary, reporter, CVE)…" value={q}
          onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        <div className="segmented" title="Filter by advisory state">
          {STATE_FILTERS.map((f) => (
            <button key={f.key} className={stateFilter === f.key ? "on" : ""}
              onClick={() => { setStateFilter(f.key); setPage(1); }}>{f.label}</button>
          ))}
        </div>
        <div className="segmented" title="Filter by the find-fixed verdict">
          {VERDICT_FILTERS.map((f) => (
            <button key={f.key} className={verdictFilter === f.key ? "on" : ""}
              onClick={() => { setVerdictFilter(f.key); setPage(1); }}>{f.label}</button>
          ))}
        </div>
        <span className="muted small">{loading ? "Loading…" : total === 0 ? "No advisories" : `${start}-${end} of ${total}`}</span>
        <button className="btn-secondary sm" disabled={page <= 1 || loading} onClick={() => setPage(page - 1)}>Prev</button>
        <button className="btn-secondary sm" disabled={page >= pages || loading} onClick={() => setPage(page + 1)}>Next</button>
      </div>
      {loading && !result && (
        <div className="explorer-loading"><span className="spinner explorer-loading-spinner" /><span className="explorer-loading-label">Loading advisories…</span></div>
      )}
      {result && (
        <div className="alerts-layout">
          <div className="table-wrap">
            <table className="grid sortable alerts-table">
              <thead><tr>
                <th onClick={() => clickSort("ghsa")}>GHSA{indicator("ghsa")}</th>
                <th onClick={() => clickSort("state")}>State{indicator("state")}</th>
                <th onClick={() => clickSort("severity")}>Severity{indicator("severity")}</th>
                <th onClick={() => clickSort("summary")}>Summary{indicator("summary")}</th>
                <th onClick={() => clickSort("reporter")}>Reporter{indicator("reporter")}</th>
                <th onClick={() => clickSort("created")} title="Time since the report was filed">Age{indicator("created")}</th>
                <th onClick={() => clickSort("verdict")} title="The find-fixed pass's verdict">Fix scan{indicator("verdict")}</th>
                <th onClick={() => clickSort("links")}>Links{indicator("links")}</th>
              </tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className={`rowlink ${selected === r.ghsa_id ? "row-selected" : ""}`} onClick={() => setSelected(r.ghsa_id)}>
                    <td className="mono small">
                      <a href={r.html_url} target="_blank" rel="noreferrer" className="gh-pr-link" title="Open on GitHub ↗" onClick={stopRowOpen}>{r.ghsa_id}</a>
                    </td>
                    <td><StateChip s={r.state} /></td>
                    <td><SeverityChip s={r.severity} /></td>
                    <td>{r.summary}</td>
                    <td className="small">{r.reporter ?? "—"}</td>
                    <td className="muted small">{timeAgo(r.created_at)}</td>
                    <td><VerdictChip r={r} /></td>
                    <td onClick={stopRowOpen}><Links r={r} /></td>
                  </tr>
                ))}
                {!loading && rows.length === 0 && (
                  <tr><td colSpan={8} className="muted">No matching advisories. Run the security sweep from the Control tab to fetch them.</td></tr>
                )}
              </tbody>
            </table>
          </div>
          {selected && <DetailPanel ghsa={selected} onClose={() => setSelected(null)} />}
        </div>
      )}
    </>
  );
}
