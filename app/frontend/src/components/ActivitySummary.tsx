import { useEffect, useMemo, useRef, useState } from "react";
import { api, type ActivitySummary as Sum, type ActivityBucket, type ActivityProgress, type IssueActivityProgress, type FirehoseStats, type ActivityPerson } from "../api";
import { useRepoMeta } from "../RepoMetaContext";

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

// ── Shared SVG chart primitives ───────────────────────────────────────────────

interface VelSeries {
  key: string;
  label: string;
  color: string;
  values: number[];
  dashed?: boolean;
}

// Daily series — cumulative is rendered separately in its own chart.
const VEL_SERIES_META = [
  { key: "pr_incoming",  label: "PRs opened",             color: "var(--gold)" },
  { key: "pr_merged",    label: "PRs merged",              color: "var(--green)" },
  { key: "pr_closed",    label: "PRs closed (no merge)",   color: "var(--accent)" },
  { key: "iss_incoming", label: "Issues opened",           color: "var(--purple)" },
  { key: "iss_closed",   label: "Issues closed",           color: "var(--muted)" },
] as const;

function buildDailySeries(fh: FirehoseStats): VelSeries[] {
  return VEL_SERIES_META.map((m) => ({
    key: m.key,
    label: m.label,
    color: m.color,
    values: fh[m.key as keyof FirehoseStats] as number[],
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
    },
    {
      key: "cumulative_issues",
      label: "Cumulative open issues (net change)",
      color: "var(--purple)",
      dashed: true,
      values: issCumulative.map((v) => v - issMin),
    },
  ];
}

// ── Reusable SVG line chart ───────────────────────────────────────────────────

function LineChart({ series, days, height = 160 }: { series: VelSeries[]; days: string[]; height?: number }) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const containerRef = useRef<HTMLDivElement>(null);

  const W = 800, H = height;
  const PAD = { top: 8, right: 10, bottom: 26, left: 36 };
  const pw = W - PAD.left - PAD.right;
  const ph = H - PAD.top - PAD.bottom;
  const n = days.length;

  const visibleSeries = series.filter((s) => !hidden.has(s.key));
  const maxVal = Math.max(1, ...visibleSeries.flatMap((s) => s.values));

  const toX = (i: number) => PAD.left + (n <= 1 ? pw / 2 : (i / (n - 1)) * pw);
  const toY = (v: number) => PAD.top + ph - Math.max(0, Math.min(1, v / maxVal)) * ph;

  const pathFor = (values: number[]) =>
    values.map((v, i) => `${i === 0 ? "M" : "L"} ${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(" ");

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
        {days.map((d, i) => {
          if (i % labelStep !== 0 && i !== n - 1) return null;
          return (
            <text key={d} x={toX(i)} y={H - 5} textAnchor="middle"
              fill="var(--muted)" fontSize={8.5}>
              {d.slice(5)}
            </text>
          );
        })}
        {series.filter((s) => !hidden.has(s.key)).map((s) => (
          <path key={s.key} d={pathFor(s.values)}
            stroke={s.color} fill="none" strokeWidth={1.5}
            strokeDasharray={s.dashed ? "5,3" : undefined}
            strokeLinecap="round" strokeLinejoin="round" />
        ))}
        {hoverIdx !== null && (
          <line x1={toX(hoverIdx)} y1={PAD.top} x2={toX(hoverIdx)} y2={PAD.top + ph}
            stroke="var(--muted)" strokeWidth={0.5} strokeDasharray="3,3" />
        )}
        {hoverIdx !== null && series.filter((s) => !hidden.has(s.key)).map((s) => (
          <circle key={s.key} cx={toX(hoverIdx)} cy={toY(s.values[hoverIdx])}
            r={3} fill={s.color} />
        ))}
      </svg>

      {hoverIdx !== null && (
        <div className="vel-tooltip">
          <div className="vel-tooltip-date">{days[hoverIdx]}</div>
          {series.map((s) => {
            const isHidden = hidden.has(s.key);
            return (
              <div key={s.key} className={`vel-tooltip-row${isHidden ? " vel-tooltip-row-hidden" : ""}`}>
                <span className="vel-swatch" style={{ background: s.color, opacity: isHidden ? 0.3 : 1 }} />
                <span className="vel-tooltip-label">{s.label}</span>
                <span className="vel-tooltip-val">{isHidden ? "—" : s.values[hoverIdx]}</span>
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

  const W = 800, H = height;
  const PAD = { top: 8, right: 10, bottom: 26, left: 36 };
  const pw = W - PAD.left - PAD.right;
  const ph = H - PAD.top - PAD.bottom;
  const n = days.length;

  const visibleSeries = series.filter((s) => !hidden.has(s.key));

  // Stacked bar: sum all visible series per day
  const stackTotals = days.map((_, i) => visibleSeries.reduce((sum, s) => sum + (s.values[i] ?? 0), 0));
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
        {days.map((d, i) => {
          if (i % labelStep !== 0 && i !== n - 1) return null;
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
                const v = s.values[i] ?? 0;
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
            return (
              <div key={s.key} className={`vel-tooltip-row${isHidden ? " vel-tooltip-row-hidden" : ""}`}>
                <span className="vel-swatch" style={{ background: s.color, opacity: isHidden ? 0.3 : 1 }} />
                <span className="vel-tooltip-label">{s.label}</span>
                <span className="vel-tooltip-val">{isHidden ? "—" : s.values[hoverIdx]}</span>
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
  n: number;
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

  // Derive operator name and PR author login from the selected person.
  const who = selectedPerson?.is_operator ? selectedPerson.display : "";
  const prAuthor = selectedPerson?.login ?? "";

  useEffect(() => {
    api.activityPeople().then((r) => setPeople(r.people));
    api.activityProgress().then(setProg);
    api.activityIssueProgress().then(setIssueProg);
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset firehose to loading before refetching on range or author change
    setFirehose(undefined);
    setReopenedPage(0);
    api.activityFirehose(rangeOpt.days, rangeOpt.allTime, prAuthor || undefined).then(setFirehose);
  }, [rangeOpt, prAuthor]);

  useEffect(() => {
    const p = who ? { operator: who } : {};
    api.activitySummary({ group_by: "day", ...p }).then(setDay);
  }, [who]);

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
  const reopened = firehose?.reopened_after_close ?? [];
  const reopenedPageCount = Math.max(1, Math.ceil(reopened.length / REOPENED_PAGE_SIZE));
  const reopenedClampedPage = Math.min(reopenedPage, reopenedPageCount - 1);
  const reopenedStart = reopenedClampedPage * REOPENED_PAGE_SIZE;
  const reopenedShown = reopened.slice(reopenedStart, reopenedStart + REOPENED_PAGE_SIZE);
  const issActionCounts = firehose?.iss_action_counts ?? {};
  const dailySeries = firehose ? buildDailySeries(firehose) : null;
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
            value={selectedPerson?.login ?? ""}
            onChange={(e) => {
              const login = e.target.value;
              setSelectedPerson(login ? (people.find((p) => p.login === login) ?? null) : null);
            }}
          >
            <option value="">everyone</option>
            {operators.length > 0 && (
              <optgroup label="Triage operators">
                {operators.map((p) => (
                  <option key={p.login} value={p.login}>{p.display}</option>
                ))}
              </optgroup>
            )}
            {nonOperators.length > 0 && (
              <optgroup label="PR authors">
                {nonOperators.map((p) => (
                  <option key={p.login} value={p.login}>{p.login} ({p.pr_count})</option>
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

      {issueProg && issueProg.universe > 0 && (
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
      <div className="act-cards-section">
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
      </div>

      {/* PR stat cards from upstream GitHub firehose */}
      <div className="act-cards-section">
        <div className="act-cards-label muted small">Upstream · PRs</div>
        <div className="act-cards">
          {fhTotals ? (
            <>
              <StatCard n={fhTotals.pr_incoming_7d} label="PRs opened this week"
                color="var(--gold)" />
              <StatCard n={fhTotals.pr_incoming_nd} label={`PRs opened (${rangeOpt.label})`}
                color="var(--gold)" muted />
            </>
          ) : (
            <>
              <StatCard n={0} label="PRs opened this week" color="var(--gold)" muted />
              <StatCard n={0} label={`PRs opened (${rangeOpt.label})`} color="var(--gold)" muted />
            </>
          )}
        </div>
      </div>

      {/* Issue stat cards from upstream GitHub firehose */}
      <div className="act-cards-section">
        <div className="act-cards-label muted small">Upstream · Issues</div>
        <div className="act-cards">
          {fhTotals ? (
            <>
              <StatCard n={fhTotals.iss_incoming_7d} label="issues opened this week"
                color="var(--purple)" />
              <StatCard n={fhTotals.iss_incoming_nd} label={`issues opened (${rangeOpt.label})`}
                color="var(--purple)" muted />
            </>
          ) : (
            <>
              <StatCard n={0} label="issues opened this week" color="var(--purple)" muted />
              <StatCard n={0} label={`issues opened (${rangeOpt.label})`} color="var(--purple)" muted />
            </>
          )}
        </div>
      </div>

      {/* Velocity line/bar chart: upstream daily PR and issue activity */}
      <div className="act-firehose">
        <div className="act-firehose-head">
          <span className="act-chart-title muted small">upstream daily activity</span>
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
                  <div className="act-breakdown-item">
                    <div className="act-breakdown-n" style={{ color: "var(--gold)" }}>{fhTotals?.pr_incoming_nd ?? 0}</div>
                    <div className="act-breakdown-l">Opened</div>
                  </div>
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
              <div className="act-breakdown-divider" />
              <div className="act-breakdown-section">
                <div className="act-breakdown-label muted small">Issues</div>
                <div className="act-breakdown-grid">
                  <div className="act-breakdown-item">
                    <div className="act-breakdown-n" style={{ color: "var(--purple)" }}>{fhTotals?.iss_incoming_nd ?? 0}</div>
                    <div className="act-breakdown-l">Opened</div>
                  </div>
                </div>
              </div>
            </div>

            {/* Daily activity + cumulative charts side by side */}
            {cumulativeSeries && (
              <div className="act-charts-row">
                <div className="act-charts-col">
                  <div className="act-chart-title muted small" style={{ marginBottom: 8 }}>
                    cumulative open PRs · net change over window
                  </div>
                  <LineChart series={[cumulativeSeries[0]]} days={firehose.days} height={100} />
                </div>
                <div className="act-charts-col">
                  <div className="act-chart-title muted small" style={{ marginBottom: 8 }}>
                    cumulative open issues · net change over window
                  </div>
                  <LineChart series={[cumulativeSeries[1]]} days={firehose.days} height={100} />
                </div>
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
          {who ? `${who}'s actions since ${weekStart}:` : `all operators since ${weekStart}:`}
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
