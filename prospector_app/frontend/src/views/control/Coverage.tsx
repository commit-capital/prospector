import type { PipelineStatus } from "../../api";
import { Panel } from "./Panel";
import { ago, agoColor, fmt } from "./format";

/** One phase's status at a glance: freshness chip up top, done/total progress
 * bar + pending count below. Omitting `done`/`total` renders freshness only.
 * Passing `stale` switches to the freshness-honest reading: `done` counts only
 * facts computed against each PR's present head, and the pending side splits
 * into stale (a later push outdated the fact) vs never covered. */
function PhaseCard({ icon, label, lastRun, done, total, totalLabel, stale }: {
  icon: string;
  label: string;
  lastRun: string | null | undefined;
  done?: number;
  total?: number;
  totalLabel?: string;
  stale?: number;
}) {
  const hasCoverage = done !== undefined && total !== undefined && total > 0;
  const pending = hasCoverage ? total - done : 0;
  const pct = hasCoverage ? Math.round((done / total) * 100) : null;
  const never = hasCoverage && stale !== undefined ? pending - stale : undefined;
  return (
    <div className="phase-card">
      <div className="phase-card-head">
        <span className="phase-card-title"><span className="phase-card-icon">{icon}</span>{label}</span>
        <span className={agoColor(lastRun)} title={fmt(lastRun)}>{ago(lastRun)}</span>
      </div>
      {hasCoverage && (
        <>
          <div className="phase-bar">
            <div className={`phase-bar-fill${pending > 0 ? " amber" : ""}`} style={{ width: `${pct}%` }} />
          </div>
          <div className="phase-card-stats">
            <span><b>{done.toLocaleString()}</b> / {total.toLocaleString()} {totalLabel ?? "PRs"} {stale !== undefined ? "current" : "done"}</span>
            {stale !== undefined && never !== undefined ? (
              pending > 0 && (
                <span className="phase-card-pending"
                  title="stale: computed before the PR's latest push — a re-run re-covers it. never: no run has reached this PR yet.">
                  {stale > 0 && `${stale.toLocaleString()} stale`}
                  {stale > 0 && never > 0 && " · "}
                  {never > 0 && `${never.toLocaleString()} never`}
                </span>
              )
            ) : (
              pending > 0 && <span className="phase-card-pending">{pending.toLocaleString()} pending</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/** How far each pipeline phase has covered the open PRs and issues. */
export function Coverage({ pipeline }: { pipeline: PipelineStatus | null }) {
  const last = (phase: string) => pipeline?.phases.find((p) => p.phase === phase)?.last_run;
  const cov = pipeline?.coverage;
  const icov = pipeline?.issue_coverage;
  return (
    <Panel id="coverage" title="Pipeline coverage"
      meta={cov && (
        <span title={"Phases feed forward: Ingest → Threat scan → Clustering → Analysis → Security → Verify. "
          + "\"Current\" counts facts computed against each PR's latest push; a push after a run leaves that fact stale until the phase re-runs."}>
          {cov.total.toLocaleString()} open PRs{icov && ` · ${icov.open.toLocaleString()} open issues`}
          {` · ${cov.tracked.toLocaleString()} PRs tracked`}
        </span>
      )}>
      {pipeline == null ? <div className="muted small">Loading…</div> : (
        <div className="phase-grid">
          <PhaseCard icon="🛡️" label="Threat scan" lastRun={last("threat-scan")}
            done={cov?.threat.current} total={cov?.total} stale={cov?.threat.stale} />
          <PhaseCard icon="🧩" label="Clustering" lastRun={last("cluster")}
            done={cov?.clustered} total={cov?.total} />
          <PhaseCard icon="🔍" label="Analysis" lastRun={last("analyze:commit")}
            done={cov?.analysis.current} total={cov?.total} stale={cov?.analysis.stale} />
          <PhaseCard icon="🔒" label="Security review" lastRun={last("security:commit")}
            done={cov?.security.current} total={cov?.total} stale={cov?.security.stale} />
          {icov && (
            <PhaseCard icon="🐞" label="Issue analysis" lastRun={last("issue-analyze")}
              done={icov.analyzed} total={icov.open} totalLabel="open issues" />
          )}
          <div className="phase-card">
            <div className="phase-card-head">
              <span className="phase-card-title"><span className="phase-card-icon">📥</span>Ingest</span>
            </div>
            <div className="phase-card-stats" style={{ justifyContent: "flex-start", gap: 14 }}>
              <span>PRs <span className={agoColor(last("ingest"))} title={fmt(last("ingest"))}>{ago(last("ingest"))}</span></span>
              <span>Issues <span className={agoColor(last("issue-ingest"))} title={fmt(last("issue-ingest"))}>{ago(last("issue-ingest"))}</span></span>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}
