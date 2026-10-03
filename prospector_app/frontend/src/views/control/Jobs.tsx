import { useState, type ReactNode } from "react";
import { api, type JobRuntime, type JobSpec, type PipelineStatus, type SuggestedAction } from "../../api";
import { Panel } from "./Panel";
import { ago, fmtDuration } from "./format";
import { layoutJobs } from "./jobLayout";

const REFRESH_LIVE = "refresh-live";
const SCAN_RESPONSES = "scan-responses";
const EXTRAS: { kind: string; label: string }[] = [
  { kind: REFRESH_LIVE, label: "Refresh live PR state" },
  { kind: SCAN_RESPONSES, label: "Scan for responses" },
];

/** Rough wall-clock projections for the job rows whose workload scales with
 *  the backlog, from recent runs-ledger history; every other row shows its
 *  ledger-typical whole-run duration. */
function duration(spec: JobSpec, count: string, runtime: JobRuntime | undefined,
  pipeline: PipelineStatus | null): string | null {
  const est = pipeline?.estimates;
  const n = Number(count);
  if (spec.kind === "threat-scan") return fmtDuration(est?.threat_scan_seconds);
  if (spec.kind === "analyze-clusters") {
    const cov = pipeline?.coverage;
    return cov && est?.analyze_clusters_seconds_per_cluster != null && Number.isFinite(n) && n > 0
      ? fmtDuration(est.analyze_clusters_seconds_per_cluster * Math.min(n, cov.analysis.never)) : null;
  }
  if (spec.kind === "issue-analyze") {
    const icov = pipeline?.issue_coverage;
    return icov && est?.issue_analyze_seconds_per_issue != null && Number.isFinite(n) && n > 0
      ? fmtDuration(est.issue_analyze_seconds_per_issue * Math.min(n, icov.pending_analysis)) : null;
  }
  const typical = fmtDuration(runtime?.typical_seconds);
  return typical && `${typical}${runtime?.typical_count != null && spec.count_noun
    ? ` for ${Math.round(runtime.typical_count)} ${spec.count_noun}` : ""}`;
}

/** The backlog a job would work through, where the pipeline status counts one. */
function backlogNote(spec: JobSpec, pipeline: PipelineStatus | null): ReactNode {
  const cov = pipeline?.coverage;
  const icov = pipeline?.issue_coverage;
  if (spec.kind === "threat-scan" && cov && cov.threat.stale + cov.threat.never > 0) {
    return (
      <>
        {(cov.threat.stale + cov.threat.never).toLocaleString()} PRs lack a scan of their latest push
        ({cov.threat.never.toLocaleString()} never scanned, {cov.threat.stale.toLocaleString()} pushed to since their scan).
        {cov.threat.diff_uncached_here > 0
          ? <> A run here first fetches {cov.threat.diff_uncached_here.toLocaleString()} open-PR
            diff{cov.threat.diff_uncached_here === 1 ? "" : "s"} this machine hasn't cached, so it may take longer.</>
          : <> All of their diffs are already cached on this machine.</>}
      </>
    );
  }
  if (spec.kind === "analyze-clusters" && cov && cov.analysis.never > 0) {
    return (
      <>
        {cov.analysis.never.toLocaleString()} PRs haven't been analyzed yet. Analysis works per cluster, so it
        only reaches clustered PRs{cov.not_clustered > 0 && <> — {cov.not_clustered.toLocaleString()} PRs are
        unclustered until Clustering runs</>}.
      </>
    );
  }
  if (spec.kind === "issue-analyze" && icov && icov.pending_analysis > 0) {
    return <>{icov.pending_analysis.toLocaleString()} open issues have no disposition yet.</>;
  }
  return null;
}

/** Every Control-tab job. The jobs worth running now come first, full width
 *  with why; the rest are tiles that open one at a time. */
export function Jobs({ specs, runtimes, suggestions, pipeline, running, onStart }: {
  specs: JobSpec[];
  runtimes: Record<string, JobRuntime> | null;
  suggestions: SuggestedAction[];
  pipeline: PipelineStatus | null;
  running: string | null;
  onStart: (url: string, spec: JobSpec) => void;
}) {
  const [cluster, setCluster] = useState("");
  const [prNum, setPrNum] = useState("");
  const [counts, setCounts] = useState<Record<string, string>>({});
  const [openKind, setOpenKind] = useState<string | null>(null);
  const [inputError, setInputError] = useState<{ kind: string; msg: string } | null>(null);
  const [recon, setRecon] = useState<{ checked: number; changed: number; failed: number[]; complete: boolean } | null>(null);
  const [reconBusy, setReconBusy] = useState(false);
  const [resp, setResp] = useState<{ checked: number; with_response: number; failed: number[] } | null>(null);
  const [respBusy, setRespBusy] = useState(false);

  const { recommended, rest } = layoutJobs(specs, suggestions);
  const suggestionFor = (kind: string) => suggestions.find((s) => s.kind === kind);
  const countFor = (spec: JobSpec): string =>
    counts[spec.kind] ?? String(suggestionFor(spec.kind)?.count ?? spec.count_default ?? "");

  const run = (spec: JobSpec) => {
    if (running) return;
    const fail = (msg: string) => setInputError({ kind: spec.kind, msg });
    if (spec.needs_cluster && !cluster) return fail("Enter a cluster id first.");
    if (spec.needs_pr && !prNum) return fail("Enter a PR number first.");
    if (spec.needs_count && !(Number(countFor(spec)) >= 1)) return fail(`Enter how many ${spec.count_noun ?? "items"} to take on.`);
    setInputError(null);
    const params = spec.needs_cluster ? `?cluster=${encodeURIComponent(cluster)}`
      : spec.needs_pr ? `?pr=${encodeURIComponent(prNum)}`
      : spec.needs_count ? `?count=${encodeURIComponent(countFor(spec))}` : "";
    onStart(`/api/jobs/run/${spec.kind}${params}`, spec);
  };

  const sweepReconcile = async () => {
    setReconBusy(true);
    try { setRecon(await api.refreshLive()); } finally { setReconBusy(false); }
  };
  const scanResponses = async () => {
    setRespBusy(true);
    try { setResp(await api.scanResponses()); } finally { setRespBusy(false); }
  };

  const detail = (spec: JobSpec, suggestion?: SuggestedAction) => {
    const runtime = runtimes?.[spec.kind];
    const takes = duration(spec, countFor(spec), runtime, pipeline);
    const note = suggestion ? null : backlogNote(spec, pipeline);
    return (
      <div key={spec.kind} className={suggestion ? "job-recommended" : "job-open"}>
        <div className="job-row-head">
          <b>{spec.label}</b>
          {suggestion && <span className="chip chip-amber sm">recommended</span>}
          <span className="muted small">
            {spec.agentic
              ? <span title="runs AI agents — costs tokens">🤖 agentic</span>
              : <span title="no agents — costs nothing but time">⚙️ deterministic</span>}
            {runtimes && <> · last run {ago(runtime?.last_run)}</>}
            {takes && <> · takes {takes}</>}
          </span>
          <span style={{ flex: 1 }} />
          {spec.needs_cluster && (
            <input className="search sm" placeholder="cluster #" value={cluster} onChange={(e) => setCluster(e.target.value)} />
          )}
          {spec.needs_pr && (
            <input className="search sm" placeholder="PR #" value={prNum} onChange={(e) => setPrNum(e.target.value)} />
          )}
          {spec.needs_count && (
            <input className="search sm" type="number" min={1} style={{ width: 72 }}
              placeholder={`# ${spec.count_noun ?? "items"}`} aria-label={`How many ${spec.count_noun ?? "items"}`}
              title={spec.count_noun ? `How many ${spec.count_noun} this run takes on` : undefined}
              value={countFor(spec)}
              onChange={(e) => setCounts((c) => ({ ...c, [spec.kind]: e.target.value }))} />
          )}
          <button className="btn-secondary sm" disabled={running !== null} onClick={() => run(spec)}>
            {running === spec.kind ? "Running…" : "▶ Run"}
          </button>
        </div>
        {suggestion && <div className="job-row-detail" style={{ color: "var(--text)" }}>{suggestion.reason}</div>}
        <div className="job-row-detail">{spec.detail}</div>
        {note && <div className="job-row-detail">{note}</div>}
        {inputError?.kind === spec.kind && <div className="job-row-detail activity-bad">{inputError.msg}</div>}
      </div>
    );
  };

  const extraDetail = (kind: string) => kind === REFRESH_LIVE ? (
    <div className="job-open">
      <div className="job-row-head">
        <b>Refresh live PR state</b>
        <span style={{ flex: 1 }} />
        <button className="btn-secondary sm" onClick={sweepReconcile} disabled={reconBusy}>
          {reconBusy ? "Refreshing…" : "♻️ Refresh"}
        </button>
      </div>
      <div className="job-row-detail">
        Re-fetch every open PR's upstream status (open/closed/merged) from GitHub into the shared store. Runs on
        launch; this re-runs it now. Read-only upstream.
        {recon && <> · last attempt: {recon.changed} changed of {recon.checked} checked
          {!recon.complete && <span className="chip chip-red sm" style={{ marginLeft: 6 }}>⚠ {recon.failed.length} could not be checked</span>}</>}
      </div>
    </div>
  ) : (
    <div className="job-open">
      <div className="job-row-head">
        <b>Scan for community responses</b>
        <span style={{ flex: 1 }} />
        <button className="btn-secondary sm" onClick={scanResponses} disabled={respBusy}>
          {respBusy ? "Scanning…" : "🔔 Scan"}
        </button>
      </div>
      <div className="job-row-detail">
        Check every PR we've acted on for how the author responded since (replies, reopens, new commits). Surfaces
        in the PR Explorer as the Updated column.
        {resp && <> · last scan: {resp.with_response} responded of {resp.checked} checked
          {resp.failed.length > 0 && <span className="chip chip-red sm" style={{ marginLeft: 6 }}>⚠ {resp.failed.length} could not be checked (gh error) — their prior status was kept</span>}</>}
      </div>
    </div>
  );

  const tiles: { kind: string; label: string }[] = [...rest, ...EXTRAS];
  const openSpec = rest.find((s) => s.kind === openKind);
  return (
    <Panel id="jobs" title="Run a job"
      meta={<>{recommended.length > 0 ? `${recommended.length} recommended` : "nothing recommended right now"}
        {" "}· each runs server-side and keeps going if you leave · 🤖 agentic jobs cost tokens</>}>
      {recommended.map((r) => detail(r.spec, r.suggestion))}
      <div className="job-tiles" style={recommended.length > 0 ? { marginTop: 12 } : undefined}>
        {tiles.map((t) => (
          <button key={t.kind} className={`job-tile${openKind === t.kind ? " open" : ""}`}
            aria-expanded={openKind === t.kind} title={t.label}
            onClick={() => setOpenKind((k) => (k === t.kind ? null : t.kind))}>
            <span className="job-tile-name">{t.label}</span>
            {running === t.kind
              ? <span className="job-tile-running">running…</span>
              : runtimes?.[t.kind]?.last_run && <span className="muted small">{ago(runtimes[t.kind].last_run)}</span>}
          </button>
        ))}
      </div>
      {openSpec ? detail(openSpec) : openKind != null && EXTRAS.some((e) => e.kind === openKind) && extraDetail(openKind)}
    </Panel>
  );
}
