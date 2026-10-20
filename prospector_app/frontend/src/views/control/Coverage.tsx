import type { PipelineStatus } from "../../api";
import { Panel } from "./Panel";
import { ago, agoColor, fmt } from "./format";
import { PIPELINE_STEPS, pipelineStep } from "./jobLayout";

/** One phase's status at a glance: freshness chip up top, done/total progress
 * bar + pending count below. Omitting `done`/`total` renders freshness only.
 * Passing `stale` splits the pending count into stale (a later push outdated
 * the fact) vs never covered, and reads `done` as facts current at each PR's
 * present head. */
function PhaseCard({ icon, label, step, lastRun, done, total, totalLabel, stale }: {
  icon: string;
  label: string;
  step?: number | null;
  lastRun: string | null | undefined;
  done?: number;
  total?: number;
  totalLabel?: string;
  stale?: number;
}) {
  const hasCoverage = done !== undefined && total !== undefined && total > 0;
  const pending = hasCoverage ? total - done : 0;
  const pct = hasCoverage ? Math.round((done / total) * 100) : null;
  const never = stale !== undefined ? pending - stale : undefined;
  return (
    <div className="phase-card">
      <div className="phase-card-head">
        <span className="phase-card-title">
          {step != null && <span className="phase-step" title={`Step ${step} of the PR pipeline`}>{step}</span>}
          <span className="phase-card-icon">{icon}</span>{label}
        </span>
        <span className={agoColor(lastRun)} title={fmt(lastRun)}>{ago(lastRun)}</span>
      </div>
      {hasCoverage && (
        <>
          <div className="phase-bar">
            <div className={`phase-bar-fill${pending > 0 ? " amber" : ""}`} style={{ width: `${pct}%` }} />
          </div>
          <div className="phase-card-stats">
            <span><b>{done.toLocaleString()}</b> / {total.toLocaleString()} {totalLabel ?? "PRs"} {stale !== undefined ? "current" : "done"}</span>
            {pending > 0 && (
              <span className="phase-card-pending"
                title="stale: computed before the PR's latest push — a re-run re-covers it. never: no run has reached this PR yet.">
                {pending.toLocaleString()} pending
                {stale !== undefined && never !== undefined && stale > 0 && never > 0
                  && ` (${stale.toLocaleString()} stale · ${never.toLocaleString()} never)`}
              </span>
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
  const scoped = cov && cov.total - cov.exempt;
  return (
    <Panel id="coverage" title="Pipeline coverage"
      meta={cov && (
        <span title={"\"Current\" counts facts computed against each PR's latest push; a push after a run leaves that fact stale until the phase re-runs. "
          + "Clustering, Analysis and Security review skip dependency bumps."}>
          {cov.total.toLocaleString()} open PRs{icov && ` · ${icov.open.toLocaleString()} open issues`}
          {` · ${cov.tracked.toLocaleString()} PRs tracked`}
          {" · run in order: "}{PIPELINE_STEPS.map((s, i) => `${i + 1} ${s.label}`).join(" → ")}
        </span>
      )}>
      {pipeline == null ? <div className="muted small">Loading…</div> : (
        <div className="phase-grid">
          <div className="phase-card">
            <div className="phase-card-head">
              <span className="phase-card-title">
                <span className="phase-step" title="Step 1 of the PR pipeline">1</span>
                <span className="phase-card-icon">📥</span>Ingest
              </span>
            </div>
            <div className="phase-card-stats" style={{ justifyContent: "flex-start", gap: 14 }}>
              <span>PRs <span className={agoColor(last("ingest"))} title={fmt(last("ingest"))}>{ago(last("ingest"))}</span></span>
              <span>Issues <span className={agoColor(last("issue-ingest"))} title={fmt(last("issue-ingest"))}>{ago(last("issue-ingest"))}</span></span>
            </div>
          </div>
          <PhaseCard icon="🛡️" label="Threat scan" step={pipelineStep("threat-scan")} lastRun={last("threat-scan")}
            done={cov?.threat.current} total={cov?.total} stale={cov?.threat.stale} />
          <PhaseCard icon="🧩" label="Clustering" step={pipelineStep("cluster-new")} lastRun={last("cluster")}
            done={cov?.cluster.current} total={scoped} />
          <PhaseCard icon="🔍" label="Analysis" step={pipelineStep("analyze-clusters")} lastRun={last("analyze:commit")}
            done={cov?.analysis.current} total={scoped} stale={cov?.analysis.stale} />
          <PhaseCard icon="🔒" label="Security review" step={pipelineStep("security-review")} lastRun={last("security:commit")}
            done={cov?.security.current} total={scoped} stale={cov?.security.stale} />
          {icov && (
            <PhaseCard icon="🐞" label="Issue analysis" lastRun={last("issue-analyze")}
              done={icov.analyzed} total={icov.open} totalLabel="open issues" />
          )}
        </div>
      )}
    </Panel>
  );
}
