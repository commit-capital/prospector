import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { api, type JobSpec, type JobRec, type PipelineStatus, type Autohunt, type AutohuntResultCounts, type FilterSpec } from "../api";
import { useRepoMeta } from "../RepoMetaContext";
import { PRLink } from "../components/PRLink";

const HUNT_RANGE_OPTIONS = [
  { label: "7 days", days: 7, allTime: false },
  { label: "30 days", days: 30, allTime: false },
  { label: "90 days", days: 90, allTime: false },
  { label: "all time", days: 7, allTime: true },
] as const;
type HuntRangeOpt = (typeof HUNT_RANGE_OPTIONS)[number];

function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.floor(ms / 60_000);
  if (min < 2) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const d = Math.floor(hr / 24);
  return `${d}d ago`;
}

function fmt(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16).replace("+00", " UTC");
}

/** A rough "~Ns"/"~Nm"/"~Nh" wall-clock estimate, or null when there isn't
 *  enough run history yet (or the projected count is zero) to show one. */
function fmtDuration(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return null;
  if (seconds < 60) return `~${Math.max(1, Math.round(seconds))}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `~${minutes < 10 ? minutes.toFixed(1) : Math.round(minutes)}m`;
  const hours = minutes / 60;
  return `~${hours < 10 ? hours.toFixed(1) : Math.round(hours)}h`;
}

function agoColor(iso: string | null | undefined): string {
  if (!iso) return "chip chip-muted";
  const hr = (Date.now() - new Date(iso).getTime()) / 3_600_000;
  if (hr < 48) return "chip chip-green";
  if (hr < 168) return "chip chip-amber";
  return "chip chip-red";
}

/** Chip tone for a hunt run's result: security verdicts map straight to their
 *  color; verify outcomes are green when the fix is confirmed, red on
 *  error/regression, amber for every in-between state. */
function resultChip(phase: string, result: string | null | undefined): string {
  if (!result) return "chip chip-muted";
  if (phase === "security") {
    if (result === "GREEN") return "chip chip-green";
    if (result === "YELLOW") return "chip chip-amber";
    if (result === "RED") return "chip chip-red";
    return "chip chip-muted";
  }
  if (result === "verified-fix" || result === "agent-verified") return "chip chip-green";
  if (result.startsWith("error") || result === "regressed") return "chip chip-red";
  return "chip chip-amber";
}

/** One phase's status at a glance: freshness chip up top, done/total progress
 * bar + pending count below. Omitting `done`/`total` renders freshness only
 * (used by phases like ingest that don't have a per-PR completion state).
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

/** One lane's (security or verify) result digest for the auto-hunt summary:
 * a headline run count plus a chip row breaking it down by result. Each chip
 * with a known PR set opens exactly those PRs in the Explorer. */
function HuntLaneSummary({ phase, counts }: { phase: "security" | "verify"; counts: AutohuntResultCounts }) {
  const entries = Object.entries(counts.by_result).sort((a, b) => b[1] - a[1]);
  return (
    <div className="act-card" style={{ flex: "1 1 220px" }}>
      <div className="act-card-n">{counts.total}</div>
      <div className="act-card-l">{phase === "security" ? "🛡 security reviews" : "🧪 verify runs"}</div>
      {entries.length > 0 && (
        <div className="small" style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 4 }}>
          {entries.map(([result, n]) => {
            const ids = counts.pr_ids_by_result[result] ?? [];
            if (ids.length === 0) return <span key={result} className={resultChip(phase, result)}>{result} {n}</span>;
            // state: "all" — a hunt result often outlives the PR (verified-fix
            // PRs get merged soon after), so the explicit PR set must survive
            // the Explorer's default open-only filter.
            const spec: FilterSpec = { numbers: ids, state: "all" };
            return (
              <Link key={result} to={`/explore?spec=${encodeURIComponent(JSON.stringify(spec))}`}
                className={resultChip(phase, result)} style={{ cursor: "pointer" }}
                title={`Open ${ids.length} PR${ids.length === 1 ? "" : "s"} with this ${phase} result in PR Explorer`}>
                {result} {n}
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function ControlPanel() {
  const { meta: repoMeta } = useRepoMeta();
  const [specs, setSpecs] = useState<JobSpec[]>([]);
  const [jobs, setJobs] = useState<JobRec[]>([]);
  const [cluster, setCluster] = useState("");
  const [prNum, setPrNum] = useState("");
  const [analyzeCount, setAnalyzeCount] = useState("200"); // issues per issue-analyze run
  const [clusterAnalyzeCount, setClusterAnalyzeCount] = useState("20"); // clusters per analyze-clusters run
  const [log, setLog] = useState<string[]>([]);
  const [running, setRunning] = useState<string | null>(null); // kind of running job
  const esRef = useRef<EventSource | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [learn, setLearn] = useState<{ count: number; with_reason: number; decisions: Record<string, number> } | null>(null);
  const [recon, setRecon] = useState<{ checked: number; changed: number } | null>(null);
  const [reconBusy, setReconBusy] = useState(false);
  const [resp, setResp] = useState<{ checked: number; with_response: number; failed: number[] } | null>(null);
  const [respBusy, setRespBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [hunt, setHunt] = useState<Autohunt | null>(null);
  const [huntRange, setHuntRange] = useState<HuntRangeOpt>(HUNT_RANGE_OPTIONS[0]);
  const [huntExpanded, setHuntExpanded] = useState(false);

  const refreshJobs = () => api.jobsList().then((d) => setJobs(d.jobs)).catch((e) => setErr(String(e)));
  const refreshPipelineStatus = () => api.pipelineStatus().then(setPipeline).catch(() => {});

  // A job started here keeps running server-side for as long as it takes,
  // independent of this page (#683) — attach (or reattach) an SSE stream to
  // it and drive the shared `log`/`running` state from whichever job kind
  // it is. Safe to abandon: closing this connection (tab close, navigating
  // away) never touches the job itself, only this page's view of it.
  const attachStream = (url: string, kind: string) => {
    esRef.current?.close();
    setRunning(kind);
    const es = new EventSource(url);
    esRef.current = es;
    es.addEventListener("log", (e: MessageEvent) => setLog((l) => [...l, e.data]));
    es.addEventListener("done", (e: MessageEvent) => {
      const { status } = JSON.parse(e.data);
      setLog((l) => [...l, `■ job ${status}`]);
      es.close(); setRunning(null); refreshJobs(); refreshPipelineStatus();
    });
    es.onerror = () => { es.close(); setRunning(null); refreshJobs(); refreshPipelineStatus(); };
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- clear the error before reloading control data
    setErr(undefined);
    api.jobSpecs().then((d) => setSpecs(d.specs)).catch((e) => setErr(String(e)));
    api.jobsList().then((d) => {
      setJobs(d.jobs);
      // #683: reattach to the most recent job (running or already finished)
      // instead of showing an empty console just because this is a fresh
      // page load — the operator's question is "what did that job do", which
      // applies whether or not it's still going.
      const latest = d.jobs.slice().sort((a, b) => b.id - a.id)[0];
      if (latest) {
        setLog([`↻ reattached to job #${latest.id} (${latest.label}) — replaying its output so far…`]);
        attachStream(`/api/jobs/${latest.id}/stream`, latest.kind);
      }
    }).catch((e) => setErr(String(e)));
    refreshPipelineStatus();
    api.trainingStats().then(setLearn).catch(() => {});
    return () => esRef.current?.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only: reattach check runs once
  }, []);

  useEffect(() => {
    const load = () => api.autohunt(huntRange.days, huntRange.allTime).then(setHunt).catch(() => {});
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [huntRange]);

  const sweepReconcile = async () => {
    setReconBusy(true);
    try { setRecon(await api.refreshLive()); } finally { setReconBusy(false); }
  };

  const scanResponses = async () => {
    setRespBusy(true);
    try { setResp(await api.scanResponses()); } finally { setRespBusy(false); }
  };

  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight); }, [log]);

  // needs_count is shared by more than one job kind (issues vs. clusters), each
  // with its own backlog size and default — pick the field that matches.
  const countFor = (kind: string) => (kind === "analyze-clusters" ? clusterAnalyzeCount : analyzeCount);

  const run = (spec: JobSpec) => {
    if (running) return;
    if (spec.needs_cluster && !cluster) { setLog((l) => [...l, "⚠ enter a cluster id first"]); return; }
    if (spec.needs_pr && !prNum) { setLog((l) => [...l, "⚠ enter a PR number first"]); return; }
    if (spec.needs_count && !(Number(countFor(spec.kind)) >= 1)) { setLog((l) => [...l, "⚠ enter how many to analyze"]); return; }
    setLog([`▶ starting ${spec.label}…`]);
    const params = spec.needs_cluster ? `?cluster=${encodeURIComponent(cluster)}`
      : spec.needs_pr ? `?pr=${encodeURIComponent(prNum)}`
      : spec.needs_count ? `?count=${encodeURIComponent(countFor(spec.kind))}` : "";
    attachStream(`/api/jobs/run/${spec.kind}${params}`, spec.kind);
  };

  const runByKind = (kind: string) => {
    const spec = specs.find((s) => s.kind === kind);
    if (spec) run(spec);
  };

  const cov = pipeline?.coverage;
  const icov = pipeline?.issue_coverage;
  const est = pipeline?.estimates;

  // Rough wall-clock estimates for the Quick Action buttons below, from recent
  // runs-ledger history. null (rendered as nothing) until a phase has enough
  // history to project from — see pipeline_status.py's `estimates`.
  const estIngest = fmtDuration(est?.ingest_seconds);
  const estThreatScan = cov && est?.threat_scan_seconds_per_pr != null
    ? fmtDuration(est.threat_scan_seconds_per_pr * cov.total) : null;
  const clusterCountNum = Number(clusterAnalyzeCount);
  const estAnalyzeClusters = cov && est?.analyze_clusters_seconds_per_cluster != null
      && Number.isFinite(clusterCountNum) && clusterCountNum > 0
    ? fmtDuration(est.analyze_clusters_seconds_per_cluster * Math.min(clusterCountNum, cov.analysis.never))
    : null;
  const issueCountNum = Number(analyzeCount);
  const estIssueAnalyze = icov && est?.issue_analyze_seconds_per_issue != null
      && Number.isFinite(issueCountNum) && issueCountNum > 0
    ? fmtDuration(est.issue_analyze_seconds_per_issue * Math.min(issueCountNum, icov.pending_analysis))
    : null;

  return (
    <div className="control">
      {err && !specs.length && (
        <div className="error">Couldn't load pipeline jobs: {err}</div>
      )}

      <div className="detail-head">
        <h1>🎛️ Control panel</h1>
        <p className="muted">
          Pipeline status, freshness, and quick actions. All jobs are read-only against
          {" "}{repoMeta?.repo ?? "the upstream repo"}. Full re-clustering rebuilds every cluster row in the store and is run manually.
        </p>
      </div>

      {/* ── Pipeline Status ── */}
      <h3 style={{ marginTop: 0, marginBottom: 4 }}>Pipeline status</h3>

      {pipeline ? (
        <>
          {cov && (
            <p className="muted small" style={{ margin: "0 0 10px" }}>
              {cov.total.toLocaleString()} PRs tracked
              {icov && ` · ${icov.open.toLocaleString()} open issues`}
              {" "}· phases feed forward: Ingest → Threat scan → Clustering → Analysis → Security → Verify.
              {" "}"Current" counts facts computed against each PR's latest push; a push after a run leaves that fact stale until the phase re-runs.
            </p>
          )}

          <div className="phase-grid">
            <div className="phase-card">
              <div className="phase-card-head">
                <span className="phase-card-title"><span className="phase-card-icon">📥</span>Ingest</span>
              </div>
              <div className="phase-card-stats">
                <span>PRs <span className={agoColor(pipeline.phases.find((p) => p.phase === "ingest")?.last_run)}
                  title={fmt(pipeline.phases.find((p) => p.phase === "ingest")?.last_run)}>
                  {ago(pipeline.phases.find((p) => p.phase === "ingest")?.last_run)}
                </span></span>
                <span>Issues <span className={agoColor(pipeline.phases.find((p) => p.phase === "issue-ingest")?.last_run)}
                  title={fmt(pipeline.phases.find((p) => p.phase === "issue-ingest")?.last_run)}>
                  {ago(pipeline.phases.find((p) => p.phase === "issue-ingest")?.last_run)}
                </span></span>
              </div>
            </div>
            <PhaseCard icon="🛡️" label="Threat scan"
              lastRun={pipeline.phases.find((p) => p.phase === "threat-scan")?.last_run}
              done={cov?.threat.current} total={cov?.total} stale={cov?.threat.stale} />
            <PhaseCard icon="🧩" label="Clustering"
              lastRun={pipeline.phases.find((p) => p.phase === "cluster:commit")?.last_run}
              done={cov?.clustered} total={cov?.total} />
            <PhaseCard icon="🔍" label="Analysis"
              lastRun={pipeline.phases.find((p) => p.phase === "analyze:commit")?.last_run}
              done={cov?.analysis.current} total={cov?.total} stale={cov?.analysis.stale} />
            <PhaseCard icon="🔒" label="Security review"
              lastRun={pipeline.phases.find((p) => p.phase === "security:commit")?.last_run}
              done={cov?.security.current} total={cov?.total} stale={cov?.security.stale} />
            {icov && (
              <PhaseCard icon="🐞" label="Issue analysis"
                lastRun={pipeline.phases.find((p) => p.phase === "issue-analyze")?.last_run}
                done={icov.analyzed} total={icov.open} totalLabel="open issues" />
            )}
          </div>
        </>
      ) : (
        <div className="muted small" style={{ marginBottom: 14 }}>Loading pipeline status…</div>
      )}

      {/* ── Quick Actions ── */}
      <h3>Quick actions</h3>
      <div className="jobspecs">
        {/* Ingest */}
        {specs.filter((s) => s.kind === "ingest").map((s) => (
          <div className="jobspec" key={s.kind}>
            <span className="jobspec-label">
              <b>Ingest</b> — refresh PR metadata + signals from GitHub. Cheap and safe to re-run.
              {estIngest && (
                <span className="muted small" title="averaged over recent ingest runs"> · takes {estIngest}</span>
              )}
              {pipeline?.phases.find((p) => p.phase === "ingest")?.last_run && (
                <span className="muted small"> · last run {ago(pipeline.phases.find((p) => p.phase === "ingest")?.last_run)}</span>
              )}
            </span>
            <button className="btn-secondary sm" disabled={running !== null} onClick={() => run(s)}>
              {running === s.kind ? "Running…" : "▶ Run ingest"}
            </button>
          </div>
        ))}

        {/* Threat scan */}
        {specs.filter((s) => s.kind === "threat-scan").map((s) => (
          <div className="jobspec" key={s.kind}>
            <span className="jobspec-label">
              <b>Threat scan</b> — deterministic attack-pattern scan over PR diffs + author blocklist check.
              Fetches any uncached diffs from GitHub first (read-only), so coverage doesn&apos;t wait on a Clustering run.
              {cov && estThreatScan && (
                <span className="muted small" title="every run rescans all tracked PRs, not just the unscanned ones">
                  {" "}· takes {estThreatScan} for all {cov.total.toLocaleString()} PRs
                </span>
              )}
              {pipeline?.phases.find((p) => p.phase === "threat-scan")?.last_run && (
                <span className="muted small"> · last run {ago(pipeline.phases.find((p) => p.phase === "threat-scan")?.last_run)}</span>
              )}
              {cov && cov.threat.stale + cov.threat.never > 0 && (
                <span className="muted small">
                  <br />⚠ {(cov.threat.stale + cov.threat.never).toLocaleString()} PRs lack a scan of their latest push
                  ({cov.threat.never.toLocaleString()} never scanned, {cov.threat.stale.toLocaleString()} pushed to since their scan).
                  {cov.threat.diff_uncached_here > 0 ? (
                    <> A run here fetches {cov.threat.diff_uncached_here.toLocaleString()} missing
                    diff{cov.threat.diff_uncached_here === 1 ? "" : "s"} from GitHub as it scans, so it may take longer.</>
                  ) : (
                    <> All of their diffs are already cached on this machine.</>
                  )}
                </span>
              )}
            </span>
            <button className="btn-secondary sm" disabled={running !== null} onClick={() => run(s)}>
              {running === s.kind ? "Running…" : "▶ Run threat scan"}
            </button>
          </div>
        ))}

        {/* Analyze backlog note */}
        {cov && cov.analysis.never > 0 && (
          <div className="jobspec" style={{ background: "color-mix(in srgb, var(--gold) 8%, var(--panel))", borderColor: "var(--gold)" }}>
            <span className="jobspec-label">
              <b>Analysis backlog</b> — {cov.analysis.never.toLocaleString()} PRs haven't been analyzed yet.
              Each run analyzes the lowest-id pending clusters in parallel (gh reads only,
              nothing upstream). Analysis works per cluster, so it only reaches clustered
              PRs{cov.not_clustered > 0 && (
                <> — {cov.not_clustered.toLocaleString()} PRs are unclustered until Clustering runs</>
              )}.
              {estAnalyzeClusters && (
                <span className="muted small" title="averaged over recent analyze-clusters runs">
                  {" "}· takes {estAnalyzeClusters} for {Math.min(clusterCountNum, cov.analysis.never).toLocaleString()} clusters
                </span>
              )}
              {pipeline?.phases.find((p) => p.phase === "analyze:commit")?.last_run && (
                <span className="muted small"> · last run {ago(pipeline.phases.find((p) => p.phase === "analyze:commit")?.last_run)}</span>
              )}
            </span>
            <input className="search sm" type="number" min={1} style={{ width: 72 }}
              value={clusterAnalyzeCount} onChange={(e) => setClusterAnalyzeCount(e.target.value)}
              title="How many pending clusters to analyze this run" />
            <button className="btn-secondary sm" disabled={running !== null} onClick={() => runByKind("analyze-clusters")}>
              {running === "analyze-clusters" ? "Running…" : "▶ Analyze clusters"}
            </button>
            <button className="btn-secondary sm" disabled={running !== null}
              onClick={() => runByKind("triage-cluster")}
              title="Triage one specific cluster (also refreshes its PRs from GitHub first) — enter a cluster ID below in the full job runner">
              Triage one cluster ↓
            </button>
          </div>
        )}

        {/* Issue analysis backlog */}
        {icov && icov.pending_analysis > 0 && (
          <div className="jobspec" style={{ background: "color-mix(in srgb, var(--gold) 8%, var(--panel))", borderColor: "var(--gold)" }}>
            <span className="jobspec-label">
              <b>Issue analysis backlog</b> — {icov.pending_analysis.toLocaleString()} open issues have no
              disposition yet. Each run analyzes the lowest-id pending issues in parallel batches (store
              writes only, nothing upstream).
              {estIssueAnalyze && (
                <span className="muted small" title="averaged over recent issue-analyze runs">
                  {" "}· takes {estIssueAnalyze} for {Math.min(issueCountNum, icov.pending_analysis).toLocaleString()} issues
                </span>
              )}
              {pipeline?.phases.find((p) => p.phase === "issue-analyze")?.last_run && (
                <span className="muted small"> · last run {ago(pipeline.phases.find((p) => p.phase === "issue-analyze")?.last_run)}</span>
              )}
            </span>
            <input className="search sm" type="number" min={1} style={{ width: 72 }}
              value={analyzeCount} onChange={(e) => setAnalyzeCount(e.target.value)}
              title="How many pending issues to analyze this run" />
            <button className="btn-secondary sm" disabled={running !== null} onClick={() => runByKind("issue-analyze")}>
              {running === "issue-analyze" ? "Running…" : "▶ Analyze issues"}
            </button>
          </div>
        )}

        {/* Refresh live PR state */}
        <div className="jobspec">
          <span className="jobspec-label">
            <b>Refresh live PR state</b> — re-fetch every open PR's upstream status (open/closed/merged) from GitHub
            into this machine's overlay. Runs on launch; this re-runs it now. Read-only.
            {recon && <span className="muted small"> · last sweep: {recon.changed} changed of {recon.checked} checked</span>}
          </span>
          <button className="btn-secondary sm" onClick={sweepReconcile} disabled={reconBusy}>
            {reconBusy ? "Refreshing…" : "♻️ Refresh live state"}
          </button>
        </div>

        {/* Scan for community responses */}
        <div className="jobspec">
          <span className="jobspec-label">
            <b>Scan for community responses</b> — check every PR we've acted on for how the author responded since
            (replies, reopens, new commits). Surfaces in the PR Explorer as the Updated column.
            {resp && (
              <span className="muted small">
                {" "}· last scan: {resp.with_response} responded of {resp.checked} checked
                {resp.failed.length > 0 && (
                  <span className="chip chip-red sm" style={{ marginLeft: 6 }}>
                    ⚠ {resp.failed.length} could not be checked (gh error) — their prior status was kept
                  </span>
                )}
              </span>
            )}
          </span>
          <button className="btn-secondary sm" onClick={scanResponses} disabled={respBusy}>
            {respBusy ? "Scanning…" : "🔔 Scan for responses"}
          </button>
        </div>
      </div>

      {/* ── Full job runner ── */}
      <h3>Job runner</h3>
      <div className="jobspecs">
        {specs.map((s) => (
          <div className="jobspec" key={s.kind}>
            <span className="jobspec-label">{s.label}</span>
            {s.needs_cluster && (
              <input className="search sm" placeholder="cluster #" value={cluster} onChange={(e) => setCluster(e.target.value)} />
            )}
            {s.needs_pr && (
              <input className="search sm" placeholder="PR #" value={prNum} onChange={(e) => setPrNum(e.target.value)} />
            )}
            {s.needs_count && (
              <input className="search sm" type="number" min={1}
                placeholder={s.kind === "analyze-clusters" ? "# clusters" : "# issues"}
                value={countFor(s.kind)}
                onChange={(e) => (s.kind === "analyze-clusters" ? setClusterAnalyzeCount : setAnalyzeCount)(e.target.value)} />
            )}
            <button className="btn-secondary sm" disabled={running !== null} onClick={() => run(s)}>
              {running === s.kind ? "Running…" : "Run"}
            </button>
          </div>
        ))}
      </div>

      <h3>Live output</h3>
      <div className="joblog" ref={logRef}>
        {log.length === 0 ? <span className="muted small">No job running. Click Run above.</span>
          : log.map((l, i) => <div key={i} className="logline">{l}</div>)}
      </div>

      {/* ── Auto-hunt ── */}
      <h3>Auto-hunt</h3>
      {hunt ? (
        <>
          <div className="jobspec" style={{ alignItems: "flex-start", flexDirection: "column", gap: 6 }}>
            <span className="jobspec-label">
              {hunt.status.enabled
                ? <span className="chip chip-green">enabled on {hunt.status.runner.host ?? "?"}</span>
                : <span className="chip chip-muted">not enabled</span>}
              {" "}
              {hunt.status.runner.online
                ? <span className="chip chip-green">runner online</span>
                : <span className="chip chip-red">runner offline</span>}
              {hunt.status.runner.current_pr != null && (
                <>{" "}<PRLink n={hunt.status.runner.current_pr} className="chip chip-blue">running #{hunt.status.runner.current_pr}</PRLink></>
              )}
            </span>
            <span className="muted small">
              {hunt.status.security_pool} awaiting security · {hunt.status.verify_pool} GREEN awaiting verify
            </span>
            {(hunt.status.security_failed.length > 0 || hunt.status.verify_failed.length > 0) && (
              <span className="small">
                <span className="muted small">needs attention (auto-run failed): </span>
                {hunt.status.security_failed.map((n) => (
                  <PRLink key={`sec-${n}`} n={n} className="chip chip-red sm">🛡 #{n}</PRLink>
                ))}
                {hunt.status.verify_failed.map((f) => (
                  <PRLink key={`ver-${f.pr}`} n={f.pr} className="chip chip-red sm">
                    🧪 #{f.pr}{f.error_kind ? ` · ${f.error_kind}` : ""}
                  </PRLink>
                ))}
              </span>
            )}
          </div>

          <div className="jobspec-label" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 12 }}>
            <span className="muted small">
              result summary · {hunt.summary.days == null ? "all time" : `last ${hunt.summary.days} days`}
            </span>
            <div className="act-range-picker">
              {HUNT_RANGE_OPTIONS.map((opt) => (
                <button key={opt.label}
                  className={`act-range-btn${huntRange.label === opt.label ? " act-range-btn-active" : ""}`}
                  onClick={() => setHuntRange(opt)}>
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          <div className="act-cards" style={{ marginTop: 6 }}>
            <HuntLaneSummary phase="security" counts={hunt.summary.security} />
            <HuntLaneSummary phase="verify" counts={hunt.summary.verify} />
          </div>

          <button className="btn-secondary sm" style={{ marginTop: 4 }}
            onClick={() => setHuntExpanded((v) => !v)}>
            {huntExpanded ? "▾ Hide individual runs" : `▸ Show individual runs (${hunt.history.length})`}
          </button>

          {huntExpanded && (
            <>
              <table className="grid compact" style={{ marginTop: 10 }}>
                <thead><tr><th>When</th><th>Lane</th><th>PR</th><th>Result</th><th>Source</th></tr></thead>
                <tbody>
                  {hunt.history.length === 0 ? (
                    <tr><td colSpan={5} className="muted small">No security or verify runs in this window.</td></tr>
                  ) : hunt.history.map((r, i) => (
                    <tr key={`${r.phase}-${r.pr}-${r.started ?? i}`}>
                      <td className="muted small" title={fmt(r.finished ?? r.started)}>{ago(r.finished ?? r.started)}</td>
                      <td>{r.phase === "security" ? "🛡 security" : "🧪 verify"}</td>
                      <td className="mono">
                        <PRLink n={r.pr} />
                        {r.title && <div className="muted small">{r.title.slice(0, 60)}</div>}
                      </td>
                      <td><span className={resultChip(r.phase, r.result)}>{r.result ?? "—"}</span></td>
                      <td><span className="chip chip-muted sm">{r.trigger === "autohunt" ? "auto" : "manual"}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {hunt.history.length < hunt.summary.security.total + hunt.summary.verify.total && (
                <div className="muted small" style={{ marginTop: 4 }}>
                  showing the latest {hunt.history.length} of {hunt.summary.security.total + hunt.summary.verify.total} runs in
                  this window — narrow the range above to see everything.
                </div>
              )}
            </>
          )}
        </>
      ) : (
        <div className="muted small" style={{ marginBottom: 14 }}>Loading auto-hunt status…</div>
      )}

      <h3>Recent jobs</h3>
      <table className="grid compact">
        <thead><tr><th>#</th><th>Job</th><th>Target</th><th>Status</th><th>Started</th></tr></thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td className="mono">{j.id}</td>
              <td>{j.label}</td>
              <td className="mono">{j.cluster != null ? `cluster ${j.cluster}` : j.pr != null ? `PR #${j.pr}` : "—"}</td>
              <td><span className={`chip ${j.status === "done" ? "chip-green" : j.status === "failed" ? "chip-red" : "chip-amber"}`}
                title={j.status === "done" ? "The job finished successfully." : j.status === "failed" ? "The job errored — check Live output above." : "The job is still in progress."}>{j.status}</span></td>
              <td className="muted small">{j.started?.slice(11, 19)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="learn-box">
        🧠 <b>Decision capture for agent learning</b> — every review action records the PR's features
        (size, checks, safety, drift, author trust) + your decision + an optional private reason,
        to <code>training/decisions.jsonl</code>.
        {learn && <span className="muted small"> · {learn.count} decisions captured ({learn.with_reason} with a private reason)
          {Object.keys(learn.decisions).length > 0 && <> · {Object.entries(learn.decisions).map(([k, v]) => `${v} ${k}`).join(", ")}</>}</span>}
      </div>
    </div>
  );
}
