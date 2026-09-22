import { useEffect, useId, useMemo, useRef, useState } from "react";
import { api, type ActivitySummary as Sum, type ActivityBucket, type ActivityProgress, type IssueActivityProgress, type FirehoseStats, type ActivityPerson, type ActivityScopeParams } from "../api";
import { useRepoMeta } from "../RepoMetaContext";
import { localDateTime, timeAgo } from "../timeAgo";

const RANGE_OPTIONS = [
  { label: "7 days", days: 7, allTime: false },
  { label: "30 days", days: 30, allTime: false },
  { label: "90 days", days: 90, allTime: false },
  { label: "all time", days: 30, allTime: true },
] as const;

type RangeOpt = (typeof RANGE_OPTIONS)[number];

const REASON_LABEL: Record<string, string> = { duplicate: "dup", "already-fixed": "fixed", stale: "stale", oversized: "oversized", manual: "manual" };
const REOPENED_PAGE_SIZE = 10;

const CHART_KINDS = ["close", "merge", "comment", "reopen"] as const;
const WEEK_KINDS = [...CHART_KINDS, "issue-close"] as const;
const num = (b: ActivityBucket | undefined, k: string) => (b ? Number(b[k] ?? 0) : 0);

// The local calendar day of `d` as YYYY-MM-DD. Not toISOString(), which is UTC:
// in the evening that's already tomorrow's date, shifting the whole axis a day
// ahead of the backend's local-day buckets.
function localDay(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

function lastNDays(n: number): string[] {
  const out: string[] = [];
  const today = new Date();
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    out.push(localDay(d));
  }
  return out;
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "";
  return iso.slice(0, 10);
}

// The index of the first chart day with no ingested data — the day after the
// last successful ingest. null when every day is covered, or when the corpus
// has never been ingested at all (there is no fresh region to contrast with).
function staleFromIndex(days: string[], asOf: string | null | undefined): number | null {
  if (!asOf) return null;
  const t = Date.parse(asOf);
  if (Number.isNaN(t)) return null;
  const asOfDay = localDay(new Date(t));
  const idx = days.findIndex((d) => d > asOfDay);
  return idx === -1 ? null : idx;
}

// A day label renders on every labelStep-th day plus the final day; a step
// label within half a step of the final one is dropped so the two never
// collide at the axis's right edge.
function showDayLabel(i: number, n: number, labelStep: number): boolean {
  if (i === n - 1) return true;
  return i % labelStep === 0 && n - 1 - i >= Math.ceil(labelStep / 2);
}

/** "as of ingest 3h" marker for a chart or count built from ingested data. */
function AsOfIngest({ stamp }: { stamp: string | null }) {
  return (
    <span className="muted small"
      title={stamp ? `last successful ingest: ${localDateTime(stamp)}` : "no successful ingest recorded"}>
      {stamp ? `as of ingest ${timeAgo(stamp)}` : "no ingest yet"}
    </span>
  );
}

// ── Shared SVG chart primitives ───────────────────────────────────────────────

interface VelSeries {
  key: string;
  label: string;
  color: string;
  values: number[];
  dashed?: boolean;
  // Index of the first day past the series' last successful ingest. Values
  // from here on are absences, not zeros: the line/bars stop and the region
  // is hatched. undefined/null when every day carries real data.
  staleFrom?: number | null;
}

// Daily series — cumulative is rendered separately in its own chart. `ingest`
// names the corpus whose ingest stamp bounds the series' data; the triage-action
// series come from the app's own activity log and are always current.
const VEL_SERIES_META = [
  { key: "pr_incoming",  label: "PRs opened",             color: "var(--gold)",   ingest: "pr" },
  { key: "pr_merged",    label: "PRs merged",              color: "var(--green)",  ingest: null },
  { key: "pr_closed",    label: "PRs closed (no merge)",   color: "var(--accent)", ingest: null },
  { key: "iss_incoming", label: "Issues opened",           color: "var(--purple)", ingest: "iss" },
  { key: "iss_closed",   label: "Issues closed",           color: "var(--muted)",  ingest: null },
] as const;

function buildDailySeries(fh: FirehoseStats): VelSeries[] {
  const staleIdx = {
    pr: staleFromIndex(fh.days, fh.ingest_as_of),
    iss: staleFromIndex(fh.days, fh.issue_ingest_as_of),
  };
  return VEL_SERIES_META.map((m) => ({
    key: m.key,
    label: m.label,
    color: m.color,
    values: fh[m.key as keyof FirehoseStats] as number[],
    staleFrom: m.ingest ? staleIdx[m.ingest] : null,
  }));
}

function buildCumulativeSeries(fh: FirehoseStats): VelSeries[] {
  const prCumulative: number[] = [];
  let prRunning = 0;
  for (let i = 0; i < fh.days.length; i++) {
    prRunning += fh.pr_incoming[i] - fh.pr_merged[i] - fh.pr_closed[i];
    prCumulative.push(prRunning);
  }
  const prMin = Math.min(0, ...prCumulative);

  const issCumulative: number[] = [];
  let issRunning = 0;
  for (let i = 0; i < fh.days.length; i++) {
    issRunning += fh.iss_incoming[i] - (fh.iss_closed[i] ?? 0);
    issCumulative.push(issRunning);
  }
  const issMin = Math.min(0, ...issCumulative);

  return [
    {
      key: "cumulative_prs",
      label: "Cumulative open PRs (net change)",
      color: "#e26d5a",
      values: prCumulative.map((v) => v - prMin),
      staleFrom: staleFromIndex(fh.days, fh.ingest_as_of),
    },
    {
      key: "cumulative_issues",
      label: "Cumulative open issues (net change)",
      color: "var(--purple)",
      dashed: true,
      values: issCumulative.map((v) => v - issMin),
      staleFrom: staleFromIndex(fh.days, fh.issue_ingest_as_of),
    },
  ];
}

// ── Stale-region helpers shared by the charts ─────────────────────────────────

// A series' value at day `i`, or null past its last ingest — an absence, not a zero.
function valAt(s: VelSeries, i: number): number | null {
  return s.staleFrom != null && i >= s.staleFrom ? null : (s.values[i] ?? 0);
}

// The chart-level start of the stale region: the earliest staleFrom across the
// series. null when no series goes stale inside the window.
function chartStaleFrom(series: VelSeries[]): number | null {
  const idxs = series.map((s) => s.staleFrom).filter((v): v is number => v != null);
  return idxs.length ? Math.min(...idxs) : null;
}

/** Diagonal hatching over the days past the last successful ingest. */
function StaleHatch({ x, width, y, height, patternId }:
  { x: number; width: number; y: number; height: number; patternId: string }) {
  if (width <= 0) return null;
  return (
    <>
      <defs>
        <pattern id={patternId} width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1={0} y1={0} x2={0} y2={6} stroke="var(--muted)" strokeWidth={1} opacity={0.4} />
        </pattern>
      </defs>
      <rect x={x} y={y} width={width} height={height} fill={`url(#${patternId})`}>
        <title>after the last successful ingest — no data</title>
      </rect>
    </>
  );
}

// ── Reusable SVG line chart ───────────────────────────────────────────────────

function LineChart({ series, days, height = 160 }: { series: VelSeries[]; days: string[]; height?: number }) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const containerRef = useRef<HTMLDivElement>(null);
  const patternId = useId();

  const W = 800, H = height;
  const PAD = { top: 8, right: 10, bottom: 26, left: 36 };
  const pw = W - PAD.left - PAD.right;
  const ph = H - PAD.top - PAD.bottom;
  const n = days.length;

  const visibleSeries = series.filter((s) => !hidden.has(s.key));
  const maxVal = Math.max(1, ...visibleSeries.flatMap((s) => s.values));
  const staleFrom = chartStaleFrom(series);

  const toX = (i: number) => PAD.left + (n <= 1 ? pw / 2 : (i / (n - 1)) * pw);
  const toY = (v: number) => PAD.top + ph - Math.max(0, Math.min(1, v / maxVal)) * ph;

  // A stale series' line stops at its last real point instead of dropping to zero.
  const pathFor = (s: VelSeries) =>
    s.values.slice(0, s.staleFrom ?? s.values.length)
      .map((v, i) => `${i === 0 ? "M" : "L"} ${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(" ");

  const labelStep = Math.max(1, Math.ceil(n / 8));
  const yMax = maxVal;
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(f * yMax));

  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect || n < 2) return;
    const relX = e.clientX - rect.left;
    const ratio = relX / rect.width;
    const svgX = ratio * W;
    const idx = Math.round(((svgX - PAD.left) / pw) * (n - 1));
    setHoverIdx(Math.max(0, Math.min(n - 1, idx)));
  };

  const toggleSeries = (key: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <div className="vel-chart-wrap" ref={containerRef}
      onMouseMove={handleMouseMove} onMouseLeave={() => setHoverIdx(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", display: "block", overflow: "visible" }}>
        {yTicks.map((v) => {
          const y = toY(v);
          return (
            <g key={v}>
              <line x1={PAD.left} y1={y} x2={PAD.left + pw} y2={y}
                stroke="var(--border)" strokeWidth={0.5} />
              <text x={PAD.left - 4} y={y + 3.5} textAnchor="end"
                fill="var(--muted)" fontSize={8.5}>{v}</text>
            </g>
          );
        })}
        <line x1={PAD.left} y1={PAD.top + ph} x2={PAD.left + pw} y2={PAD.top + ph}
          stroke="var(--border)" strokeWidth={1} />
        {staleFrom !== null && (() => {
          const x0 = staleFrom <= 0 ? PAD.left : toX(staleFrom - 0.5);
          return <StaleHatch x={x0} width={PAD.left + pw - x0} y={PAD.top} height={ph} patternId={patternId} />;
        })()}
        {days.map((d, i) => {
          if (!showDayLabel(i, n, labelStep)) return null;
          return (
            <text key={d} x={toX(i)} y={H - 5} textAnchor="middle"
              fill="var(--muted)" fontSize={8.5}>
              {d.slice(5)}
            </text>
          );
        })}
        {series.filter((s) => !hidden.has(s.key)).map((s) => (
          <path key={s.key} d={pathFor(s)}
            stroke={s.color} fill="none" strokeWidth={1.5}
            strokeDasharray={s.dashed ? "5,3" : undefined}
            strokeLinecap="round" strokeLinejoin="round" />
        ))}
        {hoverIdx !== null && (
          <line x1={toX(hoverIdx)} y1={PAD.top} x2={toX(hoverIdx)} y2={PAD.top + ph}
            stroke="var(--muted)" strokeWidth={0.5} strokeDasharray="3,3" />
        )}
        {hoverIdx !== null && series.filter((s) => !hidden.has(s.key) && valAt(s, hoverIdx) !== null).map((s) => (
          <circle key={s.key} cx={toX(hoverIdx)} cy={toY(s.values[hoverIdx])}
            r={3} fill={s.color} />
        ))}
      </svg>

      {hoverIdx !== null && (
        <div className="vel-tooltip">
          <div className="vel-tooltip-date">{days[hoverIdx]}</div>
          {series.map((s) => {
            const isHidden = hidden.has(s.key);
            const v = valAt(s, hoverIdx);
            return (
              <div key={s.key} className={`vel-tooltip-row${isHidden ? " vel-tooltip-row-hidden" : ""}`}>
                <span className="vel-swatch" style={{ background: s.color, opacity: isHidden ? 0.3 : 1 }} />
                <span className="vel-tooltip-label">{s.label}</span>
                <span className="vel-tooltip-val">{isHidden ? "—" : v === null ? "no ingest" : v}</span>
              </div>
            );
          })}
        </div>
      )}

      <div className="act-legend" style={{ marginTop: 8 }}>
        {series.map((s) => {
          const isHidden = hidden.has(s.key);
          return (
            <button key={s.key} className={`vel-legend-btn${isHidden ? " vel-legend-btn-off" : ""}`}
              onClick={() => toggleSeries(s.key)}
              title={isHidden ? `Show ${s.label}` : `Hide ${s.label}`}>
              <span className="act-swatch" style={{
                background: s.color,
                opacity: isHidden ? 0.25 : 1,
                borderStyle: s.dashed ? "dashed" : "solid",
              }} />
              <span style={{ opacity: isHidden ? 0.5 : 1 }}>
                {s.label}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── SVG bar chart (view-toggle alternative to LineChart) ──────────────────────

function BarChart({ series, days, height = 160 }: { series: VelSeries[]; days: string[]; height?: number }) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const containerRef = useRef<HTMLDivElement>(null);
  const patternId = useId();

  const W = 800, H = height;
  const PAD = { top: 8, right: 10, bottom: 26, left: 36 };
  const pw = W - PAD.left - PAD.right;
  const ph = H - PAD.top - PAD.bottom;
  const n = days.length;

  const visibleSeries = series.filter((s) => !hidden.has(s.key));
  const staleFrom = chartStaleFrom(series);

  // Stacked bar: sum all visible series per day; a stale day contributes nothing.
  const stackTotals = days.map((_, i) => visibleSeries.reduce((sum, s) => sum + (valAt(s, i) ?? 0), 0));
  const maxVal = Math.max(1, ...stackTotals);

  const barW = n > 1 ? Math.max(1, (pw / n) * 0.8) : pw * 0.6;
  const barGap = n > 1 ? pw / n : pw;
  const toX = (i: number) => PAD.left + i * barGap + barGap / 2 - barW / 2;
  const toY = (v: number) => PAD.top + ph - (v / maxVal) * ph;
  const toH = (v: number) => (v / maxVal) * ph;

  const labelStep = Math.max(1, Math.ceil(n / 8));
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(f * maxVal));

  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect || n < 2) return;
    const relX = e.clientX - rect.left;
    const idx = Math.floor((relX / rect.width) * n);
    setHoverIdx(Math.max(0, Math.min(n - 1, idx)));
  };

  const toggleSeries = (key: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <div className="vel-chart-wrap" ref={containerRef}
      onMouseMove={handleMouseMove} onMouseLeave={() => setHoverIdx(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", display: "block", overflow: "visible" }}>
        {yTicks.map((v) => {
          const y = toY(v);
          return (
            <g key={v}>
              <line x1={PAD.left} y1={y} x2={PAD.left + pw} y2={y}
                stroke="var(--border)" strokeWidth={0.5} />
              <text x={PAD.left - 4} y={y + 3.5} textAnchor="end"
                fill="var(--muted)" fontSize={8.5}>{v}</text>
            </g>
          );
        })}
        <line x1={PAD.left} y1={PAD.top + ph} x2={PAD.left + pw} y2={PAD.top + ph}
          stroke="var(--border)" strokeWidth={1} />
        {staleFrom !== null && (() => {
          const x0 = PAD.left + Math.max(0, staleFrom) * barGap;
          return <StaleHatch x={x0} width={PAD.left + pw - x0} y={PAD.top} height={ph} patternId={patternId} />;
        })()}
        {days.map((d, i) => {
          if (!showDayLabel(i, n, labelStep)) return null;
          return (
            <text key={d} x={toX(i) + barW / 2} y={H - 5} textAnchor="middle"
              fill="var(--muted)" fontSize={8.5}>
              {d.slice(5)}
            </text>
          );
        })}
        {days.map((_, i) => {
          let yOffset = PAD.top + ph;
          return (
            <g key={i} opacity={hoverIdx === i ? 1 : 0.85}>
              {visibleSeries.map((s) => {
                const v = valAt(s, i) ?? 0;
                if (v === 0) return null;
                const h = toH(v);
                yOffset -= h;
                return (
                  <rect key={s.key} x={toX(i)} y={yOffset} width={barW} height={h}
                    fill={s.color} />
                );
              })}
              {hoverIdx === i && (
                <rect x={toX(i) - 1} y={PAD.top} width={barW + 2} height={ph}
                  fill="var(--text)" opacity={0.05} rx={1} />
              )}
            </g>
          );
        })}
      </svg>

      {hoverIdx !== null && (
        <div className="vel-tooltip">
          <div className="vel-tooltip-date">{days[hoverIdx]}</div>
          {series.map((s) => {
            const isHidden = hidden.has(s.key);
            const v = valAt(s, hoverIdx);
            return (
              <div key={s.key} className={`vel-tooltip-row${isHidden ? " vel-tooltip-row-hidden" : ""}`}>
                <span className="vel-swatch" style={{ background: s.color, opacity: isHidden ? 0.3 : 1 }} />
                <span className="vel-tooltip-label">{s.label}</span>
                <span className="vel-tooltip-val">{isHidden ? "—" : v === null ? "no ingest" : v}</span>
              </div>
            );
          })}
        </div>
      )}

      <div className="act-legend" style={{ marginTop: 8 }}>
        {series.map((s) => {
          const isHidden = hidden.has(s.key);
          return (
            <button key={s.key} className={`vel-legend-btn${isHidden ? " vel-legend-btn-off" : ""}`}
              onClick={() => toggleSeries(s.key)}
              title={isHidden ? `Show ${s.label}` : `Hide ${s.label}`}>
              <span className="act-swatch" style={{
                background: s.color,
                opacity: isHidden ? 0.25 : 1,
              }} />
              <span style={{ opacity: isHidden ? 0.5 : 1 }}>
                {s.label}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── Stat card ─────────────────────────────────────────────────────────────────

interface StatCardProps {
  // "—" when the tile's whole window falls after the last successful ingest,
  // where a 0 would read as real data.
  n: number | string;
  label: string;
  lead?: boolean;
  muted?: boolean;
  color?: string;
  onClick?: () => void;
}

function StatCard({ n, label, lead, muted, color, onClick }: StatCardProps) {
  const cls = [
    "act-card",
    lead ? "act-card-lead" : "",
    muted ? "act-card-muted" : "",
    onClick ? "act-card-clickable" : "",
  ].filter(Boolean).join(" ");
  return (
    <div className={cls} onClick={onClick} role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => { if (e.key === "Enter" || e.key === " ") onClick(); } : undefined}
      title={onClick ? `Click to filter the activity log to "${label}"` : undefined}>
      <div className="act-card-n" style={color ? { color } : undefined}>{n}</div>
      <div className="act-card-l">{label}</div>
    </div>
  );
}

// ── Main dashboard component ───────────────────────────────────────────────────

interface ActivitySummaryProps {
  onQuickFilter?: (kinds: string[], range: string) => void;
}

type ChartView = "line" | "bar";

/** Statistics dashboard: headline cards, a multi-series velocity
 *  chart with bar/line toggle, a cumulative open PRs + issues chart side-by-side
 *  with the daily view, an incoming-vs-triaged category breakdown, and a list
 *  of any PRs that were reopened after we closed them. */
export function ActivitySummary({ onQuickFilter }: ActivitySummaryProps) {
  const { meta, prUrl } = useRepoMeta();
  const [day, setDay] = useState<Sum>();
  const [prog, setProg] = useState<ActivityProgress>();
  const [issueProg, setIssueProg] = useState<IssueActivityProgress>();
  const [firehose, setFirehose] = useState<FirehoseStats>();
  const [people, setPeople] = useState<ActivityPerson[]>([]);
  const [selectedPerson, setSelectedPerson] = useState<ActivityPerson | null>(null);
  const [rangeOpt, setRangeOpt] = useState<RangeOpt>(RANGE_OPTIONS[1]); // default 30 days
  const [chartView, setChartView] = useState<ChartView>("line");
  const [reopenedPage, setReopenedPage] = useState(0);

  // The two picker groups are distinct scopes even when one GitHub login
  // appears in both. Operator scope attributes actions; author scope follows
  // every PR by that author through the dashboard.
  const who = selectedPerson?.is_operator ? selectedPerson.display : "";
  const prAuthor = selectedPerson && !selectedPerson.is_operator ? selectedPerson.login : "";
  const selectedKey = selectedPerson
    ? `${selectedPerson.is_operator ? "operator" : "author"}:${selectedPerson.login}`
    : "";
  const scope = useMemo<ActivityScopeParams>(() => ({
    ...(who ? { operator: who } : {}),
    ...(prAuthor ? { prAuthor } : {}),
  }), [prAuthor, who]);
  const showIssues = !prAuthor;
  const showIncoming = !who;

  useEffect(() => {
    api.activityPeople().then((r) => setPeople(r.people));
  }, []);

  useEffect(() => {
    let current = true;
    api.activityProgress(scope).then((value) => { if (current) setProg(value); });
    api.activitySummary({
      group_by: "day",
      ...(who ? { operator: who } : {}),
      ...(prAuthor ? { pr_author: prAuthor } : {}),
    }).then((value) => { if (current) setDay(value); });
    if (showIssues) {
      api.activityIssueProgress(who ? { operator: who } : {})
        .then((value) => { if (current) setIssueProg(value); });
    }
    return () => { current = false; };
  }, [prAuthor, scope, showIssues, who]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset firehose to loading before refetching on range or author change
    setFirehose(undefined);
    setReopenedPage(0);
    api.activityFirehose(rangeOpt.days, rangeOpt.allTime, scope).then(setFirehose);
  }, [rangeOpt, scope]);

  const days = useMemo(() => lastNDays(30), []);
  const byKey = useMemo(() => {
    const m: Record<string, ActivityBucket> = {};
    for (const b of day?.buckets ?? []) m[b.key] = b;
    return m;
  }, [day]);

  const weekStart = days[days.length - 7];
  const week = useMemo(() => {
    const acc: Record<string, number> = { close: 0, merge: 0, comment: 0, reopen: 0, "issue-close": 0 };
    for (const k of days.slice(-7)) for (const kind of WEEK_KINDS) acc[kind] += num(byKey[k], kind);
    return acc;
  }, [byKey, days]);

  const totals = day?.totals ?? {};
  // PR and issue kinds are disjoint in the vocabulary; split the all-time sums accordingly.
  const allPrActions = Object.entries(totals).reduce((a, [k, v]) => (k.startsWith("issue-") ? a : a + v), 0);
  const allIssueActions = Object.entries(totals).reduce((a, [k, v]) => (k.startsWith("issue-") ? a + v : a), 0);

  if (!day) return <div className="muted small pad-sm">Loading metrics…</div>;

  const fhTotals = firehose?.totals;
  // Staleness of the ingested (upstream) counts: a window that falls entirely
  // after the last successful ingest has no data, so its tile shows "—".
  const prStaleIdx = firehose ? staleFromIndex(firehose.days, firehose.ingest_as_of) : null;
  const issStaleIdx = firehose ? staleFromIndex(firehose.days, firehose.issue_ingest_as_of) : null;
  const weekStale = (idx: number | null) => firehose != null && idx !== null && idx <= firehose.days.length - 7;
  const upstreamCount = (n: number | undefined, allStale: boolean) => (allStale ? "—" : n ?? 0);
  // The chart mixes both corpora; its marker carries the older applicable stamp.
  const chartStamps = firehose
    ? [firehose.ingest_as_of, ...(showIssues ? [firehose.issue_ingest_as_of] : [])]
    : [];
  const chartIngestAt = chartStamps.length && !chartStamps.some((s) => !s)
    ? chartStamps.reduce((a, b) => (a! < b! ? a : b))
    : null;
  const reopened = firehose?.reopened_after_close ?? [];
  const reopenedPageCount = Math.max(1, Math.ceil(reopened.length / REOPENED_PAGE_SIZE));
  const reopenedClampedPage = Math.min(reopenedPage, reopenedPageCount - 1);
  const reopenedStart = reopenedClampedPage * REOPENED_PAGE_SIZE;
  const reopenedShown = reopened.slice(reopenedStart, reopenedStart + REOPENED_PAGE_SIZE);
  const issActionCounts = firehose?.iss_action_counts ?? {};
  const dailySeries = firehose
    ? buildDailySeries(firehose).filter((series) => (
      (showIssues || !series.key.startsWith("iss_"))
      && (showIncoming || !series.key.endsWith("_incoming"))
    ))
    : null;
  const cumulativeSeries = firehose ? buildCumulativeSeries(firehose) : null;

  const quickFilter = (kinds: string[], range: string) => {
    onQuickFilter?.(kinds, range);
  };

  // Operators for the person picker (filtered from people list)
  const operators = people.filter((p) => p.is_operator);
  const nonOperators = people.filter((p) => !p.is_operator);

  return (
    <div className="act-dash">
      <div className="act-dash-head">
        <h2>{meta ? `${meta.display_name} Statistics` : "Statistics"}</h2>

        {/* Unified person picker — operators first, then PR authors */}
        <label className="muted small">
          person:{" "}
          <select
            value={selectedKey}
            onChange={(e) => {
              const key = e.target.value;
              setSelectedPerson(key ? (people.find((p) => (
                `${p.is_operator ? "operator" : "author"}:${p.login}` === key
              )) ?? null) : null);
            }}
          >
            <option value="">everyone</option>
            {operators.length > 0 && (
              <optgroup label="Triage operators">
                {operators.map((p) => (
                  <option key={`operator:${p.login}`} value={`operator:${p.login}`}>{p.display}</option>
                ))}
              </optgroup>
            )}
            {nonOperators.length > 0 && (
              <optgroup label="PR authors">
                {nonOperators.map((p) => (
                  <option key={`author:${p.login}`} value={`author:${p.login}`}>{p.login} ({p.pr_count})</option>
                ))}
              </optgroup>
            )}
          </select>
        </label>
        {selectedPerson && (
          <span className="muted small">
            {selectedPerson.is_operator && <span className="chip chip-muted sm">operator</span>}
            {selectedPerson.pr_count > 0 && <span className="chip chip-muted sm">{selectedPerson.pr_count} PRs</span>}
          </span>
        )}
        {!selectedPerson && <span className="muted small">live actions only</span>}
      </div>

      {prog && prog.universe > 0 && (
        <div className="act-progress">
          <div className="act-progress-top">
            <b>{prog.actioned.toLocaleString()}</b> of {prog.universe.toLocaleString()} PRs triaged
            <span className="muted"> · {prog.pct}%</span>
            <span className="act-progress-split muted small">
              {prog.merged} merged · {prog.closed} closed
              {Object.entries(prog.by_reason).sort((a, b) => b[1] - a[1]).map(([r, n]) => (
                <span key={r} className="chip chip-muted sm">{REASON_LABEL[r] ?? r} {n}</span>
              ))}
            </span>
          </div>
          <div className="act-progress-bar" title={`${prog.remaining.toLocaleString()} remaining`}>
            <span className="act-progress-merged" style={{ width: `${(prog.merged / prog.universe) * 100}%` }} />
            <span className="act-progress-closed" style={{ width: `${(prog.closed / prog.universe) * 100}%` }} />
          </div>
        </div>
      )}

      {/* PR stat cards — clicking filters the activity log below */}
      <div className="act-cards-section">
        <div className="act-cards-label muted small">Our actions · PRs</div>
        <div className="act-cards">
          <StatCard n={week.close} label="closed this week" lead
            onClick={() => quickFilter(["close"], "7d")} />
          <StatCard n={week.merge} label="merged this week"
            onClick={() => quickFilter(["merge"], "7d")} />
          <StatCard n={week.comment} label="comments this week"
            onClick={() => quickFilter(["comment"], "7d")} />
          <StatCard n={week.reopen} label="reopens this week"
            onClick={() => quickFilter(["reopen"], "7d")} />
          <StatCard n={allPrActions} label="actions all-time" muted
            onClick={() => quickFilter([], "all")} />
        </div>
      </div>

      {showIssues && issueProg && issueProg.universe > 0 && (
        <div className="act-progress">
          <div className="act-progress-top">
            <b>{issueProg.actioned.toLocaleString()}</b> of {issueProg.universe.toLocaleString()} issues triaged
            <span className="muted"> · {issueProg.pct}%</span>
            <span className="act-progress-split muted small">
              {issueProg.closed} closed
              {Object.entries(issueProg.by_reason).sort((a, b) => b[1] - a[1]).map(([r, n]) => (
                <span key={r} className="chip chip-muted sm">{REASON_LABEL[r] ?? r} {n}</span>
              ))}
            </span>
          </div>
          <div className="act-progress-bar" title={`${issueProg.remaining.toLocaleString()} issues remaining`}>
            <span className="act-progress-closed" style={{ width: `${(issueProg.closed / issueProg.universe) * 100}%` }} />
          </div>
        </div>
      )}

      {/* Issue stat cards — our landed issue actions from the activity log */}
      {showIssues && <div className="act-cards-section">
        <div className="act-cards-label muted small">Our actions · Issues</div>
        <div className="act-cards">
          <StatCard n={week["issue-close"]} label="closed this week" lead
            onClick={() => quickFilter(["issue-close"], "7d")} />
          <StatCard n={issActionCounts["CLOSE_ISSUE_DUP"] ?? 0} label="closed as dup (all-time)"
            color="var(--accent)" muted={!issActionCounts["CLOSE_ISSUE_DUP"]} />
          <StatCard n={issActionCounts["CLOSE_ISSUE_FIXED"] ?? 0} label="closed as fixed (all-time)"
            color="var(--green)" muted={!issActionCounts["CLOSE_ISSUE_FIXED"]} />
          <StatCard n={allIssueActions} label="issue actions all-time" muted
            onClick={() => quickFilter(["issue-close"], "all")} />
        </div>
      </div>}

      {/* PR stat cards from upstream GitHub firehose */}
      {showIncoming && <div className="act-cards-section">
        <div className="act-cards-label muted small">
          Upstream · PRs{firehose && <> · <AsOfIngest stamp={firehose.ingest_as_of} /></>}
        </div>
        <div className="act-cards">
          {fhTotals ? (
            <>
              <StatCard n={upstreamCount(fhTotals.pr_incoming_7d, weekStale(prStaleIdx))}
                label="PRs opened this week" color="var(--gold)" />
              <StatCard n={upstreamCount(fhTotals.pr_incoming_nd, prStaleIdx === 0)}
                label={`PRs opened (${rangeOpt.label})`} color="var(--gold)" muted />
            </>
          ) : (
            <>
              <StatCard n={0} label="PRs opened this week" color="var(--gold)" muted />
              <StatCard n={0} label={`PRs opened (${rangeOpt.label})`} color="var(--gold)" muted />
            </>
          )}
        </div>
      </div>}

      {/* Issue stat cards from upstream GitHub firehose */}
      {showIncoming && showIssues && <div className="act-cards-section">
        <div className="act-cards-label muted small">
          Upstream · Issues{firehose && <> · <AsOfIngest stamp={firehose.issue_ingest_as_of} /></>}
        </div>
        <div className="act-cards">
          {fhTotals ? (
            <>
              <StatCard n={upstreamCount(fhTotals.iss_incoming_7d, weekStale(issStaleIdx))}
                label="issues opened this week" color="var(--purple)" />
              <StatCard n={upstreamCount(fhTotals.iss_incoming_nd, issStaleIdx === 0)}
                label={`issues opened (${rangeOpt.label})`} color="var(--purple)" muted />
            </>
          ) : (
            <>
              <StatCard n={0} label="issues opened this week" color="var(--purple)" muted />
              <StatCard n={0} label={`issues opened (${rangeOpt.label})`} color="var(--purple)" muted />
            </>
          )}
        </div>
      </div>}

      {/* Velocity line/bar chart: upstream daily PR and issue activity */}
      <div className="act-firehose">
        <div className="act-firehose-head">
          <span className="act-chart-title muted small">
            {who ? "daily triage activity" : "upstream daily activity"}
            {showIncoming && firehose && <> · <AsOfIngest stamp={chartIngestAt} /></>}
          </span>
          <div className="act-range-picker">
            {RANGE_OPTIONS.map((opt) => (
              <button
                key={opt.label}
                className={`act-range-btn${rangeOpt.label === opt.label ? " act-range-btn-active" : ""}`}
                onClick={() => setRangeOpt(opt)}
              >
                {opt.label}
              </button>
            ))}
          </div>
          {/* Chart view toggle */}
          <div className="act-chart-toggle" title="Switch between line and bar chart">
            <button
              className={`act-range-btn${chartView === "line" ? " act-range-btn-active" : ""}`}
              onClick={() => setChartView("line")}
              title="Line chart"
            >∿ line</button>
            <button
              className={`act-range-btn${chartView === "bar" ? " act-range-btn-active" : ""}`}
              onClick={() => setChartView("bar")}
              title="Bar chart"
            >▌ bar</button>
          </div>
        </div>

        {!firehose ? (
          <div className="muted small">Loading…</div>
        ) : (
          <>
            {/* Daily activity chart with view toggle */}
            {dailySeries && (
              chartView === "line"
                ? <LineChart series={dailySeries} days={firehose.days} />
                : <BarChart series={dailySeries} days={firehose.days} />
            )}

            {/* Breakdown totals for daily series */}
            <div className="act-breakdown" style={{ marginTop: 14 }}>
              <div className="act-breakdown-section">
                <div className="act-breakdown-label muted small">Pull Requests</div>
                <div className="act-breakdown-grid">
                  {showIncoming && (
                    <div className="act-breakdown-item">
                      <div className="act-breakdown-n" style={{ color: "var(--gold)" }}>{upstreamCount(fhTotals?.pr_incoming_nd, prStaleIdx === 0)}</div>
                      <div className="act-breakdown-l">Opened</div>
                    </div>
                  )}
                  <div className="act-breakdown-item">
                    <div className="act-breakdown-n" style={{ color: "var(--green)" }}>{fhTotals?.pr_merged_nd ?? 0}</div>
                    <div className="act-breakdown-l">Merged</div>
                  </div>
                  <div className="act-breakdown-item">
                    <div className="act-breakdown-n" style={{ color: "var(--accent)" }}>{fhTotals?.pr_closed_nd ?? 0}</div>
                    <div className="act-breakdown-l">Closed</div>
                  </div>
                  <div className="act-breakdown-item">
                    <div className="act-breakdown-n" style={{ color: "var(--purple)" }}>{fhTotals?.pr_reopened_nd ?? 0}</div>
                    <div className="act-breakdown-l">Reopened</div>
                  </div>
                </div>
              </div>
              {showIssues && (
                <>
                  <div className="act-breakdown-divider" />
                  <div className="act-breakdown-section">
                    <div className="act-breakdown-label muted small">Issues</div>
                    <div className="act-breakdown-grid">
                      {showIncoming && (
                        <div className="act-breakdown-item">
                          <div className="act-breakdown-n" style={{ color: "var(--purple)" }}>{upstreamCount(fhTotals?.iss_incoming_nd, issStaleIdx === 0)}</div>
                          <div className="act-breakdown-l">Opened</div>
                        </div>
                      )}
                      <div className="act-breakdown-item">
                        <div className="act-breakdown-n" style={{ color: "var(--muted)" }}>{fhTotals?.iss_closed_nd ?? 0}</div>
                        <div className="act-breakdown-l">Closed</div>
                      </div>
                    </div>
                  </div>
                </>
              )}
            </div>

            {/* Daily activity + cumulative charts side by side */}
            {cumulativeSeries && !who && (
              <div className="act-charts-row">
                <div className="act-charts-col">
                  <div className="act-chart-title muted small" style={{ marginBottom: 8 }}>
                    cumulative open PRs · net change over window
                  </div>
                  <LineChart series={[cumulativeSeries[0]]} days={firehose.days} height={100} />
                </div>
                {showIssues && (
                  <div className="act-charts-col">
                    <div className="act-chart-title muted small" style={{ marginBottom: 8 }}>
                      cumulative open issues · net change over window
                    </div>
                    <LineChart series={[cumulativeSeries[1]]} days={firehose.days} height={100} />
                  </div>
                )}
              </div>
            )}

            {/* PRs we closed that were subsequently reopened. Always shown (with an
                explicit empty state) so a zero count reads as "none reopened", not a
                missing/broken panel — reopens are rare, so empty is the common case. */}
            <div className="act-reopened">
              <div className="act-chart-title muted small" style={{ marginTop: 14 }}>
                closed by us · now open again
                <span className="chip chip-muted sm" style={{ marginLeft: 6 }}>{reopened.length}</span>
              </div>
              {reopened.length === 0 ? (
                <div className="muted small">None — no PR we've closed has been reopened upstream.</div>
              ) : (
                <>
                  <ul className="act-reopened-list">
                    {reopenedShown.map((r) => (
                      <li key={r.pr}>
                        <a href={r.url ?? prUrl(r.pr)} target="_blank" rel="noreferrer">
                          #{r.pr}
                        </a>
                        <span className="act-reopened-title" title={r.title ?? ""}>{r.title}</span>
                        <span className="muted small">
                          {r.reason ? REASON_LABEL[r.reason] ?? r.reason : "closed"} · {fmtDate(r.closed_at)}
                        </span>
                      </li>
                    ))}
                  </ul>
                  {reopenedPageCount > 1 && (
                    <div className="pager act-reopened-pager">
                      <button disabled={reopenedClampedPage === 0} onClick={() => setReopenedPage(reopenedClampedPage - 1)}>‹ Prev</button>
                      <span className="muted small">
                        {reopenedStart + 1}–{Math.min(reopenedStart + REOPENED_PAGE_SIZE, reopened.length)} of {reopened.length}
                      </span>
                      <button disabled={reopenedClampedPage >= reopenedPageCount - 1} onClick={() => setReopenedPage(reopenedClampedPage + 1)}>Next ›</button>
                    </div>
                  )}
                </>
              )}
            </div>
          </>
        )}
      </div>

      {/* Operator-filtered totals line (when a triage operator is selected) */}
      {weekStart && day && (
        <div className="muted small" style={{ marginTop: 8 }}>
          {who
            ? `${who}'s actions since ${weekStart}:`
            : prAuthor
              ? `actions on ${prAuthor}'s PRs since ${weekStart}:`
              : `all operators since ${weekStart}:`}
          {" "}
          {WEEK_KINDS.map((k) => {
            const t = week[k];
            return t > 0 ? <span key={k}>{t} {k} · </span> : null;
          })}
        </div>
      )}
    </div>
  );
}
