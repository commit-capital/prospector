import { Fragment, useState } from "react";
import { Link } from "react-router";
import type { Autohunt, AutohuntRun, FilterSpec, FixQueue } from "../../api";
import { PRLink } from "../../components/PRLink";
import { Panel } from "./Panel";
import { RANGE_OPTIONS, type RangeOpt, ago, fmt, resultChip } from "./format";
import { mergeRuns } from "./historyRows";

type Lane = AutohuntRun["phase"];
const LANE_LABEL: Record<Lane, string> = { security: "🛡 security", verify: "🧪 verify", fix: "🔧 autofix" };

/** One lane's result breakdown as chips; a chip with known PRs opens them. */
function ResultChips({ lane, counts, prs }: {
  lane: Lane;
  counts: Record<string, number>;
  prs?: Record<string, number[]>;
}) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return <span className="muted small">none</span>;
  return (
    <>
      {entries.map(([result, n]) => {
        const ids = prs?.[result] ?? [];
        if (ids.length === 0) return <span key={result} className={resultChip(lane, result)}>{result} {n}</span>;
        // state: "all" — a run often outlives its PR (verified PRs get merged
        // soon after), so the PR set must survive the Explorer's open-only default.
        const spec: FilterSpec = { numbers: ids, state: "all" };
        return (
          <Link key={result} to={`/prs/list?spec=${encodeURIComponent(JSON.stringify(spec))}`}
            className={resultChip(lane, result)}
            title={`Open ${ids.length} PR${ids.length === 1 ? "" : "s"} with this ${lane} result in PR Explorer`}>
            {result} {n}
          </Link>
        );
      })}
    </>
  );
}

/** Security, verify and autofix runs as one history, with one range for all three. */
export function RunHistory({ hunt, fixQueue, range, onRange }: {
  hunt: Autohunt | null;
  fixQueue: FixQueue | null;
  range: RangeOpt;
  onRange: (r: RangeOpt) => void;
}) {
  const [lanes, setLanes] = useState<Set<Lane>>(new Set(["security", "verify", "fix"]));
  const fixRuns = fixQueue?.history ?? [];
  const rows = mergeRuns(hunt?.history ?? [], fixRuns).filter((r) => lanes.has(r.phase));
  const fixSummary = fixQueue?.summary;
  const window = range.allTime ? "all time" : `last ${range.days} days`;
  const totals = hunt && fixSummary
    ? `${hunt.summary.security.total} security · ${hunt.summary.verify.total} verify · ${fixSummary.total} autofix · ${window}`
    : "loading…";
  const toggle = (lane: Lane) => setLanes((s) => {
    const next = new Set(s);
    if (next.has(lane)) next.delete(lane); else next.add(lane);
    return next;
  });
  const shownHunt = hunt?.history.length ?? 0;
  const huntTotal = hunt ? hunt.summary.security.total + hunt.summary.verify.total : 0;
  return (
    <Panel id="run-history" title="Run history" foldable defaultOpen={false} summary={totals} meta={window}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <div className="act-range-picker">
          {(["security", "verify", "fix"] as const).map((lane) => (
            <button key={lane} aria-pressed={lanes.has(lane)}
              className={`act-range-btn${lanes.has(lane) ? " act-range-btn-active" : ""}`}
              onClick={() => toggle(lane)}>
              {LANE_LABEL[lane]}
            </button>
          ))}
        </div>
        <div className="act-range-picker">
          {RANGE_OPTIONS.map((opt) => (
            <button key={opt.label}
              className={`act-range-btn${range.label === opt.label ? " act-range-btn-active" : ""}`}
              onClick={() => onRange(opt)}>
              {opt.label}
            </button>
          ))}
        </div>
      </div>
      {hunt && (
        <table className="grid compact" style={{ marginTop: 10 }}>
          <tbody>
            <tr><td className="small">{LANE_LABEL.security} <b>{hunt.summary.security.total}</b></td>
              <td><ResultChips lane="security" counts={hunt.summary.security.by_result} prs={hunt.summary.security.pr_ids_by_result} /></td></tr>
            <tr><td className="small">{LANE_LABEL.verify} <b>{hunt.summary.verify.total}</b></td>
              <td><ResultChips lane="verify" counts={hunt.summary.verify.by_result} prs={hunt.summary.verify.pr_ids_by_result} /></td></tr>
            {fixSummary && (
              <tr><td className="small">{LANE_LABEL.fix} <b>{fixSummary.total}</b></td>
                <td><ResultChips lane="fix" counts={fixSummary.by_result} prs={fixSummary.pr_ids_by_result} /></td></tr>
            )}
          </tbody>
        </table>
      )}
      <table className="grid compact" style={{ marginTop: 10 }}>
        <thead><tr><th>When</th><th>Lane</th><th>PR</th><th>Action</th><th>Result</th><th>Source</th></tr></thead>
        <tbody>
          {rows.length === 0 ? (
            <tr><td colSpan={6} className="muted small">No runs in this window.</td></tr>
          ) : rows.map((r, i) => (
            <Fragment key={`${r.phase}-${r.pr}-${r.finished ?? r.started ?? i}-${i}`}>
              <tr>
                <td className="muted small" title={fmt(r.finished ?? r.started)}>{ago(r.finished ?? r.started)}</td>
                <td className="small">{LANE_LABEL[r.phase]}</td>
                <td className="mono">
                  <PRLink n={r.pr} />
                  {r.title && <div className="muted small">{r.title.slice(0, 60)}</div>}
                </td>
                <td>{r.action ? <span className="chip chip-muted sm">{r.action}</span> : <span className="muted">—</span>}</td>
                <td><span className={resultChip(r.phase, r.result)}>{r.result ?? "—"}</span></td>
                <td><span className="chip chip-muted sm">{r.trigger === "autohunt" ? "auto" : "manual"}</span></td>
              </tr>
              {r.detail && (
                <tr>
                  <td />
                  <td colSpan={5} className="muted small" style={{ paddingTop: 0 }}>{r.detail}</td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
      {(shownHunt < huntTotal || (fixSummary != null && fixRuns.length < fixSummary.total)) && (
        <div className="muted small" style={{ marginTop: 4 }}>
          showing the latest {shownHunt} of {huntTotal} security and verify runs
          {fixSummary != null && <> and {fixRuns.length} of {fixSummary.total} autofix runs</>} in this window —
          narrow the range to see them all.
        </div>
      )}
    </Panel>
  );
}
