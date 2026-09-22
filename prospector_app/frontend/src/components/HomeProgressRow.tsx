import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type HomeProgress } from "../api";
import { sparklinePath, staleFromX } from "../sparkline";
import { timeAgo } from "../timeAgo";

const SPARK_W = 132;
const SPARK_H = 30;

// Ingest older than this puts the "as of ingest" note on the row.
const STALE_NOTE_MS = 24 * 3600 * 1000;

function fmtDelta(n: number): string {
  if (n === 0) return "±0";
  return `${n > 0 ? "+" : "−"}${Math.abs(Number.isInteger(n) ? n : Number(n.toFixed(1)))}`;
}

function fmtHours(h: number): string {
  return h < 48 ? `${h < 10 ? h.toFixed(1) : Math.round(h)}h` : `${(h / 24).toFixed(1)}d`;
}

// One tile: the number, a signed week-over-week delta (green when it moved the
// good way), the 30-day sparkline with its stale tail shaded, and one line of
// context. The whole tile links to the view that holds its detail.
function Tile({ label, value, delta, downIsGood, series, days, lastIngestAt, to, title, sub }: {
  label: string;
  value: string;
  delta: number | null;
  downIsGood: boolean;
  series: (number | null)[];
  days: string[];
  lastIngestAt: string | null;
  to: string;
  title: string;
  sub: string;
}) {
  const path = sparklinePath(series, SPARK_W, SPARK_H);
  const staleX = staleFromX(days, lastIngestAt, SPARK_W);
  const good = delta !== null && delta !== 0 && (delta < 0) === downIsGood;
  return (
    <Link to={to} className="act-card home-tile" title={title}>
      <div className="home-tile-top">
        <span className="act-card-n home-tile-n">{value}</span>
        {delta !== null && (
          <span className={"home-tile-delta mono small" + (good ? " good" : delta === 0 ? "" : " bad")}
            title="Last 7 days vs the 7 before">
            {fmtDelta(delta)}
          </span>
        )}
      </div>
      <svg className="home-tile-spark" width={SPARK_W} height={SPARK_H}
        viewBox={`0 0 ${SPARK_W} ${SPARK_H}`} aria-hidden="true">
        {staleX !== null && (
          <rect className="home-tile-stale" x={staleX} y={0}
            width={SPARK_W - staleX} height={SPARK_H}
            aria-label="no ingest covers this period" />
        )}
        {path && <path d={path} fill="none" />}
      </svg>
      <div className="act-card-l">{label}</div>
      <div className="muted small home-tile-sub">{sub}</div>
    </Link>
  );
}

/** The progress row atop Home: open backlog, resolved this week, escalation
 *  rate, and time to first action — each one number, a 30-day sparkline, and
 *  a week-over-week delta, linking to its detailed view. Days after the last
 *  ingest are shaded instead of drawn as zero. */
export function HomeProgressRow() {
  const [data, setData] = useState<HomeProgress | null>(null);
  const [staleNote, setStaleNote] = useState(false);
  useEffect(() => {
    api.homeProgress().then((d) => {
      setData(d);
      setStaleNote(d.last_ingest_at === null
        || Date.now() - Date.parse(d.last_ingest_at) > STALE_NOTE_MS);
    }).catch(() => {});
  }, []);
  if (!data) return null;
  const { backlog, resolved, escalation, first_action } = data;
  const escDelta = escalation.rate_7d !== null && escalation.prev_rate_7d !== null
    ? Math.round((escalation.rate_7d - escalation.prev_rate_7d) * 100) : null;
  const firstDelta = first_action.median_hours_7d !== null && first_action.prev_median_hours_7d !== null
    ? Number((first_action.median_hours_7d - first_action.prev_median_hours_7d).toFixed(1)) : null;
  return (
    <div className="home-progress">
      <Tile label="Open backlog" value={String(backlog.current)}
        delta={backlog.delta_7d} downIsGood series={backlog.series} days={data.days}
        lastIngestAt={data.last_ingest_at} to="/explore"
        title="Open PRs + open issues as the store knows them — click for the PR Explorer"
        sub="open PRs + issues" />
      <Tile label="Resolved this week" value={String(resolved.week_total)}
        delta={resolved.week_total - resolved.prev_week_total} downIsGood={false}
        series={resolved.series} days={data.days}
        lastIngestAt={data.last_ingest_at} to="/activity"
        title="Merged and closed by Prospector in the last 7 days — click for the Activity log"
        sub={`${resolved.auto_7d} on its own · ${resolved.person_7d} by a person`} />
      <Tile label="Escalation rate"
        value={escalation.rate_7d === null ? "—" : `${Math.round(escalation.rate_7d * 100)}%`}
        delta={escDelta} downIsGood series={escalation.series} days={data.days}
        lastIngestAt={data.last_ingest_at} to="/control"
        title="The share of agent decisions handed to a person (parked fixes, failed verifications, RED verdicts) — click for the Control tab"
        sub={`${escalation.escalated_7d}/${escalation.decisions_7d} decisions`} />
      <Tile label="Time to first action"
        value={first_action.median_hours_7d === null ? "—" : fmtHours(first_action.median_hours_7d)}
        delta={firstDelta} downIsGood series={first_action.series} days={data.days}
        lastIngestAt={data.last_ingest_at} to="/activity"
        title="Median time from a PR opening to Prospector's first verdict or post — click for the Activity log"
        sub={`${first_action.sampled_7d} PRs this week`} />
      {staleNote && (
        <div className="home-progress-stale muted small" role="note">
          {data.last_ingest_at === null
            ? "No ingest has run — shaded periods have no data."
            : `As of ingest ${timeAgo(data.last_ingest_at)} ago — shaded periods have no data.`}
        </div>
      )}
    </div>
  );
}
