import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api, type ClusterState, type ClusterSummary, type Disposition, type PainBreakdown } from "../api";
import { SafetyRollupChip } from "../components/Chips";
import { InfoTip } from "../components/InfoTip";
import { SuggestedActions } from "../components/SuggestedActions";
import { clusterStateEntry, dispositionEntry } from "../glossary";

// Chip label + colour per state; the plain-language explanation lives in the
// glossary (clusterStateEntry), surfaced via InfoTip — one source of wording.
const STATE_META: Record<ClusterState, { label: string; cls: string }> = {
  "ready": { label: "ready", cls: "chip-green" },
  "security-pending": { label: "security pending", cls: "chip-yellow" },
  "needs-analysis": { label: "needs analysis", cls: "chip-muted" },
  "awaiting-authors": { label: "awaiting authors", cls: "chip-blue" },
  "needs-first-party-work": { label: "needs first-party work", cls: "chip-purple" },
  "blocked-on-decision": { label: "blocked on decision", cls: "chip-purple" },
  "done": { label: "done", cls: "chip-muted" },
};

const FILTERS = ["all", "ready", "security-pending", "needs-analysis", "waiting", "done"] as const;
type Filter = typeof FILTERS[number];
const WAITING: ClusterState[] = ["awaiting-authors", "needs-first-party-work", "blocked-on-decision"];

const STATE_ORDER: Record<ClusterState, number> = {
  "ready": 0, "security-pending": 1, "needs-analysis": 2, "awaiting-authors": 3,
  "needs-first-party-work": 4, "blocked-on-decision": 5, "done": 6,
};

// All-GREEN merge clusters rank best (1) → "--"/no-merge ranks last (5), so an
// ascending Security sort surfaces the clusters that are ready to merge first.
function securityRank(c: ClusterSummary): number {
  const s = c.security;
  const total = s.green + s.yellow + s.red + (s.unknown ?? 0);
  if (total === 0) return 5;        // no merge candidate — nothing to gate ("--")
  if (s.red > 0) return 4;          // a RED merge candidate
  if ((s.unknown ?? 0) > 0) return 3; // a merge candidate not yet reviewed
  if (s.yellow > 0) return 2;       // YELLOW concerns
  return 1;                         // all GREEN → top
}

// Why a cluster has no security audit (— in the Security column), keyed on the
// state that left it without a merge candidate.
const NO_MERGE_REASON: Record<ClusterState, string> = {
  "awaiting-authors": "its PRs need author changes before any can be merged",
  "needs-first-party-work": "no contributed PR is cleanly mergeable — a first-party PR must be written",
  "blocked-on-decision": "it's blocked on a product / architecture decision",
  "needs-analysis": "it hasn't been analyzed yet (run ANALYZE)",
  "ready": "its plan routes no PR to merge — they're being closed and/or sent back to authors",
  "security-pending": "its merge candidate isn't reviewed yet",
  "done": "all of its PRs are already resolved",
};
const DISPO_ACTION: Record<string, (n: number) => string> = {
  "merge": (n) => `merge ${n}`,
  "request-changes": (n) => `ask ${n} author${n > 1 ? "s" : ""} to address requested changes`,
  "close-dup": (n) => `close ${n} duplicate${n > 1 ? "s" : ""}`,
  "close-fixed": (n) => `close ${n} already-fixed upstream`,
  "close-stale": (n) => `close ${n} stale / abandoned`,
  "needs-human": (n) => `route ${n} by hand (needs-human)`,
};
function noMergeTip(c: ClusterSummary): string {
  const reason = NO_MERGE_REASON[c.state] ?? "no PR is routed to merge";
  const actions = Object.entries(c.dispositions ?? {})
    .map(([k, v]) => DISPO_ACTION[k]?.(v) ?? `${k} ×${v}`);
  const lines = [
    "Security audit not run — it runs only on PRs routed to merge, and this cluster has none yet.",
    `Why: ${reason}.`,
  ];
  if (actions.length) lines.push("Cluster needs these actions first:\n" + actions.map((a) => `• ${a}`).join("\n"));
  return lines.join("\n");
}

function painTip(c: ClusterSummary): string {
  const b: PainBreakdown | null | undefined = c.pain_breakdown;
  if (!b) return "Community Pain Score: no data yet";
  const lines = [
    `Community Pain Score: ${(c.pain_score ?? 0).toFixed(2)}`,
    `  • Linked issue pain: ${b.issue_pain.toFixed(2)} (${b.linked_issues} issue${b.linked_issues !== 1 ? "s" : ""})`,
    `  • PR comments: ${b.pr_comments}`,
    `  • PR reactions: ${b.pr_reactions}`,
  ];
  return lines.join("\n");
}

type SortCol = "id" | "root" | "prs" | "plan" | "security" | "state" | "pain";
// every column is sortable (#180); keys may be numeric or text. "plan" sorts by
// how many PRs the cluster routes to merge, so merge-heavy clusters surface.
// "pain" sorts by Community Pain Score descending — highest community pressure first.
const SORT_KEY: Record<SortCol, (c: ClusterSummary) => number | string> = {
  id: (c) => c.cluster_id,
  root: (c) => (c.root_problem || "").toLowerCase(),
  prs: (c) => c.pr_count,
  plan: (c) => c.dispositions?.["merge"] ?? 0,
  security: securityRank,
  state: (c) => STATE_ORDER[c.state] ?? 9,
  pain: (c) => c.pain_score ?? -1,
};
// columns whose most-useful first click is descending (biggest first)
const DESC_FIRST: SortCol[] = ["prs", "plan", "pain"];

// Module-level cache: survives navigation away and back so the board shows
// stale data immediately on re-visit while a background fetch picks up changes.
let _clustersCache: ClusterSummary[] | null = null;

// The board unmounts when you open a cluster, so its view controls (filter /
// search / sort) live at module scope too — opening a cluster and coming back
// lands you on the same filtered, sorted view you left, not a reset to "all".
// saveView() is the only writer, so the mutation stays out of the component body.
const _view = { filter: "all" as Filter, q: "", sort: "state" as SortCol, dir: "asc" as "asc" | "desc" };
const saveView = (patch: Partial<typeof _view>) => Object.assign(_view, patch);

export default function ClusterBoard() {
  const navigate = useNavigate();
  const [items, setItems] = useState<ClusterSummary[]>(_clustersCache ?? []);
  const [loading, setLoading] = useState(_clustersCache === null);
  const [filter, setFilterState] = useState<Filter>(_view.filter);
  const [q, setQState] = useState(_view.q);
  const [sort, setSortState] = useState<SortCol>(_view.sort);
  const [dir, setDirState] = useState<"asc" | "desc">(_view.dir);
  const [err, setErr] = useState<string>();

  // Mirror every view-control change into the module-level cache so it persists
  // across navigation.
  const setFilter = (f: Filter) => { saveView({ filter: f }); setFilterState(f); };
  const setQ = (v: string) => { saveView({ q: v }); setQState(v); };
  const setSort = (s: SortCol) => { saveView({ sort: s }); setSortState(s); };
  const setDir = (d: "asc" | "desc" | ((d: "asc" | "desc") => "asc" | "desc")) =>
    setDirState((prev) => { const next = typeof d === "function" ? d(prev) : d; saveView({ dir: next }); return next; });

  useEffect(() => {
    api.clusters()
      .then((d) => {
        _clustersCache = d.items;
        setItems(d.items);
        setLoading(false);
      })
      .catch((e) => {
        setLoading(false);
        // Only surface the error when there is no cached data to fall back on.
        if (!_clustersCache) setErr(String(e));
      });
  }, []);

  // A "done" cluster has every PR handled — it shows only under the "done" tab
  // (kept for looking back at old work) and drops out of "all" and every other
  // filter, so the board surfaces only clusters that still need attention.
  const matches = (c: ClusterSummary, f: Filter) =>
    f === "done" ? c.state === "done" :
    c.state === "done" ? false :
    f === "all" ? true :
    f === "waiting" ? WAITING.includes(c.state) :
    c.state === f;

  const shown = useMemo(() => {
    const rows = items.filter((c) =>
      matches(c, filter) &&
      (!q || `${c.cluster_id} ${c.root_problem}`.toLowerCase().includes(q.toLowerCase())));
    const key = SORT_KEY[sort];
    const mul = dir === "asc" ? 1 : -1;
    return rows.sort((a, b) => {
      const ka = key(a), kb = key(b);
      const d = typeof ka === "string" || typeof kb === "string"
        ? String(ka).localeCompare(String(kb))
        : ka - kb;
      return (d !== 0 ? d : a.cluster_id - b.cluster_id) * mul; // stable tiebreak on id
    });
  }, [items, filter, q, sort, dir]);

  const clickSort = (col: SortCol) => {
    if (sort === col) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSort(col); setDir(DESC_FIRST.includes(col) ? "desc" : "asc"); }
  };
  const caret = (col: SortCol) => (sort === col ? (dir === "asc" ? " ▲" : " ▼") : "");

  const counts = useMemo(() => Object.fromEntries(
    FILTERS.map((f) => [f, items.filter((c) => matches(c, f)).length])
  ) as Record<Filter, number>, [items]);

  if (err) return <div className="error">Failed to load: {err}</div>;
  if (loading) return <div className="board"><div className="callout">Loading clusters…</div></div>;
  if (items.length === 0) {
    return (
      <div className="board">
        <div className="callout">
          No clusters yet. The store has PR records (see <Link to="/prs">PR Queue</Link>) but the
          CLUSTER phase hasn't run — kick it off from <Link to="/control">Control</Link> once available.
        </div>
      </div>
    );
  }

  return (
    <div className="board">
      <SuggestedActions view="prs" />
      <div className="board-controls">
        <div className="segmented">
          {FILTERS.map((f) => (
            <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
              {f} <span className="count">{counts[f]}</span>
            </button>
          ))}
        </div>
        <input className="search" placeholder="Search clusters…" value={q} onChange={(e) => setQ(e.target.value)} />
        <Link className="btn-secondary" to="/prs">All PRs →</Link>
      </div>

      <table className="grid sortable">
        <thead>
          <tr>
            <th className={`sortable-th ${sort === "id" ? "sorted" : ""}`} title="Cluster ID — click to sort" onClick={() => clickSort("id")}>#{caret("id")}</th>
            <th className={`sortable-th ${sort === "root" ? "sorted" : ""}`} title="The root problem this cluster's PRs all address. Click the row to open it; click the header to sort A→Z." onClick={() => clickSort("root")}>Root problem{caret("root")}</th>
            <th className={`sortable-th ${sort === "prs" ? "sorted" : ""}`} title="Open PRs in this cluster (incl. drafts) — click to sort" onClick={() => clickSort("prs")}>PRs{caret("prs")}</th>
            <th className={`sortable-th ${sort === "plan" ? "sorted" : ""}`} title="Per-PR dispositions from ANALYZE (merge / request-changes / close-* / needs-human). Click to sort: clusters routing the most PRs to merge first." onClick={() => clickSort("plan")}>Plan{caret("plan")}</th>
            <th className={`sortable-th ${sort === "pain" ? "sorted" : ""}`} title="Community Pain Score — aggregates linked-issue pain, PR comments, and PR reactions from real users. Higher = more community pressure. Click to sort: most painful first." onClick={() => clickSort("pain")}>Pain{caret("pain")}</th>
            <th className={`sortable-th ${sort === "security" ? "sorted" : ""}`} title="Security verdicts on the PRs routed to merge: 🟢 GREEN / 🟡 YELLOW / 🔴 RED / ❔ not yet reviewed. '—' = no PR routed to merge (nothing to gate). Click to sort: all-GREEN merge clusters first." onClick={() => clickSort("security")}>Security{caret("security")}</th>
            <th className={`sortable-th ${sort === "state" ? "sorted" : ""}`} title="Derived live from the store via the gate policy — never stored, so never stale. Click to sort: ready first." onClick={() => clickSort("state")}>State{caret("state")}</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((c) => {
            const m = STATE_META[c.state] ?? STATE_META["needs-analysis"];
            return (
              <tr key={c.cluster_id}
                className={`rowlink ${c.state === "ready" ? "row-ready" : ""}`}
                onClick={() => navigate(`/clusters/${c.cluster_id}`)}>
                <td className="mono">{c.cluster_id}</td>
                <td><span className="cluster-title">{c.root_problem?.slice(0, 110)}</span></td>
                <td className="mono">{c.pr_count}</td>
                <td className="muted small">
                  {Object.keys(c.dispositions ?? {}).length === 0 ? "—" :
                    Object.entries(c.dispositions).map(([k, v], i) => (
                      <span key={k}>
                        {i > 0 ? " · " : ""}
                        <InfoTip entry={dispositionEntry(k as Disposition)} cue={false} focusable={false}>{k}×{v}</InfoTip>
                      </span>
                    ))}
                </td>
                <td className="mono" title={painTip(c)}>
                  {c.pain_score != null && c.pain_score > 0 ? c.pain_score.toFixed(2) : "—"}
                </td>
                <td><SafetyRollupChip r={c.security} prs={c.security_prs} emptyTip={noMergeTip(c)} /></td>
                <td>
                  <InfoTip entry={clusterStateEntry(c.state)} cue={false} focusable={false}>
                    <span className={`chip ${m.cls}`}>{m.label}</span>
                  </InfoTip>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
