import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { api, type ActivityItem } from "../api";
import { useRepoMeta } from "../RepoMetaContext";
import { useExec } from "../ExecContext";
import { PRLink } from "../components/PRLink";
import { ActivitySummary } from "../components/ActivitySummary";
import { InfoTip } from "../components/InfoTip";
import { localDateTime } from "../timeAgo";
import { term } from "../glossary";

const KIND_ICON: Record<string, string> = { merge: "🔀", close: "📮", comment: "💬", reopen: "↩", undo: "⤺", handoff: "📤", reconcile: "♻️", "issue-close": "🎫" };
const KIND_TIP: Record<string, string> = {
  merge: "A PR was merged upstream as {bot}.",
  close: "A PR was closed (with a comment) as {bot} — reopenable.",
  "issue-close": "A GitHub issue was closed (with a comment) as {bot} — as a duplicate or as fixed by a merged PR.",
  comment: "A comment was posted on a PR as {bot}.",
  reopen: "A closed PR was reopened and the bot's closing comment removed.",
  undo: "A prior action was reversed.",
  handoff: "A batch of approved actions was emitted in one handoff.",
  reconcile: "Our records were reconciled with the PR's real upstream state.",
};
const STATUS_TIP: Record<string, string> = {
  executed: "The action was carried out upstream.",
  merged: "The PR was merged.",
  reopened: "The PR was reopened.",
  error: "The action failed — see Detail.",
  blocked: "Refused by the safety gate — see Detail.",
  skipped: "Skipped — no change made.",
};

// Outcome of a live action, mirroring activity.py's DONE_STATUSES / FAILED_STATUSES
// so this client-side tally and the backend summary agree on what "counts".
const DONE_STATUSES = new Set(["executed", "merged", "closed", "reopened"]);
const FAILED_STATUSES = new Set(["error", "skipped", "blocked"]);

/** Classify one event into exactly one outcome bucket. A dry-run is a preview; a
 *  live event "succeeded" when it landed upstream and "failed" when it errored,
 *  was blocked, or skipped; anything else live (e.g. a handoff batch carrying no
 *  status) is "other" — counted in the total but neither a success nor a failure. */
function outcomeOf(it: ActivityItem): "dry" | "succeeded" | "failed" | "other" {
  if (it.dry_run) return "dry";
  if (it.status && DONE_STATUSES.has(it.status)) return "succeeded";
  if (it.status && FAILED_STATUSES.has(it.status)) return "failed";
  return "other";
}

// The feed fetches only the most recent slice of the log, so tallies here cover a
// window, not all history. All-time totals live in the progress bar (activity.py:progress).
const FETCH_LIMIT = 500;

type Mode = "all" | "live" | "dry";
type Outcome = "all" | "succeeded" | "failed";
type Range = "all" | "1d" | "7d" | "30d";
const RANGE_LABEL: Record<Range, string> = { all: "all time", "1d": "today", "7d": "7 days", "30d": "30 days" };

function withinRange(at: string | undefined, range: Range): boolean {
  if (range === "all" || !at) return true;
  const days = range === "1d" ? 1 : range === "7d" ? 7 : 30;
  const t = Date.parse(at);
  return isNaN(t) ? true : Date.now() - t <= days * 86_400_000;
}

/** Toggleable facet chips with counts; empty selection = "all". */
function Facet({ label, options, selected, onToggle }:
  { label: string; options: [string, number][]; selected: Set<string>; onToggle: (v: string) => void }) {
  if (options.length <= 1) return null;
  return (
    <div className="facet">
      <span className="facet-label">{label}</span>
      {options.map(([v, n]) => (
        <button key={v} className={`chip sm facet-chip ${selected.has(v) ? "on" : ""}`} onClick={() => onToggle(v)}>
          {v} <span className="facet-count">{n}</span>
        </button>
      ))}
    </div>
  );
}

export default function Activity() {
  const { issueUrl } = useRepoMeta();
  const { dryRun, reportResult } = useExec();
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [kinds, setKinds] = useState<Set<string>>(new Set());
  const [idents, setIdents] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<Mode>("all");
  const [outcome, setOutcome] = useState<Outcome>("all");
  const [range, setRange] = useState<Range>("all");
  const [busy, setBusy] = useState<number | null>(null);
  const [err, setErr] = useState<string>();
  const [loading, setLoading] = useState(true);
  const tableRef = useRef<HTMLDivElement>(null);

  const [syncing, setSyncing] = useState(false);
  const load = () => { setErr(undefined); return api.activity(FETCH_LIMIT).then((d) => setItems(d.items)).catch((e) => setErr(String(e))).finally(() => setLoading(false)); };
  // fetch teammates' latest activity shards, then show the merged feed
  const syncTeam = () => {
    setSyncing(true);
    api.syncActivity(FETCH_LIMIT).then((d) => setItems(d.items)).catch((e) => setErr(String(e)))
      .finally(() => setSyncing(false));
  };
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/set-state-in-effect -- load() fetches the activity feed on mount

  const handleQuickFilter = (filterKinds: string[], filterRange: string) => {
    setKinds(filterKinds.length > 0 ? new Set(filterKinds) : new Set());
    setRange((filterRange as Range) || "all");
    if (filterKinds.length > 0) {
      setOutcome("succeeded");
      setMode("live");
    } else {
      setOutcome("all");
      setMode("all");
    }
    setIdents(new Set());
    // Scroll the activity table into view after state settles
    setTimeout(() => tableRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  };

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, v: string) => {
    const next = new Set(set);
    if (next.has(v)) next.delete(v);
    else next.add(v);
    setter(next);
  };

  // facet options + counts, derived from the full set
  const [kindOpts, identOpts] = useMemo(() => {
    const k: Record<string, number> = {}, id: Record<string, number> = {};
    for (const it of items) {
      k[it.kind] = (k[it.kind] ?? 0) + 1;
      const who = it.operator ?? "—";
      id[who] = (id[who] ?? 0) + 1;
    }
    const sort = (o: Record<string, number>) => Object.entries(o).sort((a, b) => b[1] - a[1]);
    return [sort(k), sort(id)] as [[string, number][], [string, number][]];
  }, [items]);

  const shown = items.filter((it) =>
    (kinds.size === 0 || kinds.has(it.kind)) &&
    (idents.size === 0 || idents.has(it.operator ?? "—")) &&
    (mode === "all" || (mode === "live" ? it.dry_run === false : it.dry_run === true)) &&
    (outcome === "all" || outcomeOf(it) === outcome) &&
    withinRange(it.at, range));

  // Split the shown rows by outcome for the count line. The per-kind breakdown of
  // successes is the tally the operator reads off ("closed N, merged M").
  const tally = { succeeded: 0, failed: 0, dry: 0, byKind: {} as Record<string, number> };
  for (const it of shown) {
    const o = outcomeOf(it);
    if (o === "succeeded") { tally.succeeded++; tally.byKind[it.kind] = (tally.byKind[it.kind] ?? 0) + 1; }
    else if (o === "failed") tally.failed++;
    else if (o === "dry") tally.dry++;
  }
  const succeededByKind = Object.entries(tally.byKind).sort((a, b) => b[1] - a[1]);

  // Truncated when the fetch hit its cap and the range reaches past our oldest held
  // event (matching events went unloaded). items are newest-first, so withinRange on
  // the last one is true exactly when the range's edge falls off our slice.
  const oldestAt = items.length ? items[items.length - 1].at : undefined;
  const windowTruncated = items.length >= FETCH_LIMIT && withinRange(oldestAt, range);

  const reopen = async (pr: number) => {
    if (!dryRun && !window.confirm(`Reopen #${pr}, remove the bot's closing comment, and withdraw any change request?`)) return;
    setBusy(pr);
    const r = await api.reopenPr(pr, dryRun);
    setBusy(null);
    reportResult(r);
    load();
  };

  const clusterNum = (it: ActivityItem) => it.cluster_id ? parseInt(it.cluster_id, 10) : it.cluster ?? null;

  if (err && !items.length) return <div className="error">Failed to load the activity log: {err}</div>;

  return (
    <div className="activity">
      <div className="detail-head">
        <h1>📋 Activity log</h1>
        <p className="muted">Everything the team has done — posts, closes, reopens, and handoff emits. Combined audit trail; filter by teammate with “who”.</p>
      </div>

      <ActivitySummary onQuickFilter={handleQuickFilter} />

      <div ref={tableRef} className="activity-filters">
        <Facet label="kind" options={kindOpts} selected={kinds} onToggle={(v) => toggle(kinds, setKinds, v)} />
        <Facet label="who" options={identOpts} selected={idents} onToggle={(v) => toggle(idents, setIdents, v)} />
        <div className="facet">
          <span className="facet-label">mode</span>
          <div className="segmented">
            {(["all", "live", "dry"] as const).map((m) => (
              <button key={m} className={mode === m ? "on" : ""} onClick={() => setMode(m)}
                title={m === "live" ? "Real actions sent upstream." : m === "dry" ? "Simulated actions — logged, never sent." : "Both live and dry-run events."}>{m}</button>
            ))}
          </div>
        </div>
        <div className="facet">
          <span className="facet-label">outcome</span>
          <div className="segmented">
            {(["all", "succeeded", "failed"] as const).map((o) => (
              <button key={o} className={outcome === o ? "on" : ""} onClick={() => setOutcome(o)}
                title={o === "succeeded" ? "Only actions that landed upstream." : o === "failed" ? "Only attempts that errored, were blocked, or skipped." : "All outcomes — successes and failures."}>{o}</button>
            ))}
          </div>
        </div>
        <div className="facet">
          <span className="facet-label">when</span>
          <div className="segmented">
            {(["all", "1d", "7d", "30d"] as const).map((r) => (
              <button key={r} className={range === r ? "on" : ""} onClick={() => setRange(r)} title={RANGE_LABEL[r]}>{r}</button>
            ))}
          </div>
        </div>
        <button className="btn-secondary sm" onClick={load}>↻ refresh</button>
        <button className="btn-secondary sm" onClick={syncTeam} disabled={syncing}
          title="Fetch teammates' latest activity from the shared shards — no manual git pull.">
          {syncing ? "syncing…" : "⟳ sync team"}</button>
      </div>

      {(!loading || items.length > 0) && (
      <div className="muted small activity-count">
        {shown.length} shown
        {windowTruncated && (
          <span title={`These counts cover only the most recent ${FETCH_LIMIT} logged events — older actions are off-screen, so this is not an all-time total. For all-time disposition counts see the progress bar above.`}>
            {" "}<span className="chip chip-muted sm">most recent {FETCH_LIMIT}</span>
          </span>
        )}
        {" · "}{tally.succeeded} succeeded
        {succeededByKind.length > 0 && (
          <span> ({succeededByKind.map(([k, n]) => `${n} ${k}`).join(" · ")})</span>
        )}
        {" · "}{tally.failed} failed
        {tally.dry > 0 ? ` · ${tally.dry} dry` : ""}
        {(kinds.size || idents.size || mode !== "all" || outcome !== "all" || range !== "all") ? (
          <button className="linkish" onClick={() => { setKinds(new Set()); setIdents(new Set()); setMode("all"); setOutcome("all"); setRange("all"); }}>clear filters</button>
        ) : null}
      </div>
      )}

      {loading && !items.length && (
        <div className="explorer-loading">
          <span className="spinner explorer-loading-spinner" />
          <span className="explorer-loading-label">Loading activity…</span>
        </div>
      )}

      <table className="grid compact" style={loading && !items.length ? { display: "none" } : undefined}>
        <thead><tr><th>When</th><th>Kind</th><th>Who</th><th>Target</th><th>Cluster</th><th>Status</th><th>Detail</th><th></th></tr></thead>
        <tbody>
          {shown.map((it, i) => {
            const cn = clusterNum(it);
            return (
            <tr key={i} className={it.dry_run ? "row-skipped" : ""}>
              <td className="muted small mono" title={it.at}>{localDateTime(it.at)}</td>
              <td>
                <InfoTip entry={KIND_TIP[it.kind] ? { title: it.kind, meaning: KIND_TIP[it.kind] } : null} cue={false} focusable={false}>
                  <span>{KIND_ICON[it.kind] ?? "•"} {it.kind}</span>
                </InfoTip>
                {it.dry_run && <InfoTip entry={term("dry-run")} cue={false} focusable={false}><span className="chip chip-muted sm">dry</span></InfoTip>}
              </td>
              <td className="muted small" title={it.identity ? `posted as ${it.identity}` : undefined}>{it.operator ?? it.identity ?? "—"}</td>
              <td className="mono">
                {it.pr ? <PRLink n={it.pr} />
                  : it.issue ? (
                    <a href={issueUrl(it.issue)}
                      target="_blank" rel="noreferrer">#{it.issue}</a>
                  ) : "—"}
                {it.action && <span className="muted small"> {it.action}</span>}
                {it.reason && <span className="chip chip-muted sm">{it.reason}</span>}
              </td>
              <td className="mono">{cn != null ? <Link to={`/clusters/${cn}`}>{it.cluster_id ?? cn}</Link> : "—"}</td>
              <td><span className={`chip chip-${it.status === "executed" || it.status === "reopened" || it.status === "merged" ? "green" : it.status === "error" ? "red" : "muted"}`} title={it.status ? STATUS_TIP[it.status] : undefined}>{it.status ?? (it.kind === "handoff" ? `${it.approved_count} actions` : "")}</span></td>
              <td className="muted small">{it.detail}</td>
              <td>
                {it.kind === "close" && it.status === "executed" && it.pr && (
                  <button className="btn-secondary sm" disabled={busy === it.pr} onClick={() => reopen(it.pr!)}>
                    {busy === it.pr ? "…" : "↩ undo"}
                  </button>
                )}
              </td>
            </tr>
          ); })}
        </tbody>
      </table>
      {!loading && shown.length === 0 && <div className="callout">No matching activity. {items.length > 0 ? "Try clearing filters." : "Posting/closing/emitting shows up here."}</div>}
    </div>
  );
}
