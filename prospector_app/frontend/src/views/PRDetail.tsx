import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type PRDetail as PD, type DiffResult, type VerifyRunner, type FixAction as FixActionName, type FixRunner, type ReviewDigest, type ReviewEntry, type ReviewFinding, type CoverageRecord } from "../api";
import { authorTip } from "../components/Chips";
import { ChecksPanel } from "../components/ChecksPanel";
import { DiffView } from "../components/DiffView";
import { MergeSideBySide } from "../components/MergeSideBySide";
import { CHECKS_ID, checkRowId, diffFileId, flashTo, jumpToDiff } from "../components/pageAnchor";
import { Collapsible } from "../components/Collapsible";
import { bannerFindings } from "../components/bannerFindings";
import { PRActionBar } from "../components/PRActionBar";
import { AnalysisLine, PRStory, type AnalysisTarget } from "../components/PRStory";
import { VerifyAction, VerifyBody } from "../components/VerifyPanel";
import { FixAction, FixActionButton, FixBody } from "../components/FixPanel";
import { designFixGoal, unavailable as fixUnavailable } from "../components/fixAvailability";
import { withLiveConflicts } from "../components/liveChecks";
import { ThreatEvidencePanel } from "../components/ThreatEvidencePanel";
import { PRLink } from "../components/PRLink";
import { coverageLabel, coverageTone } from "../dupCoverage";
import { PRHistory } from "../components/PRHistory";
import { LineCommentBox } from "../components/ReviewModal";
import { useExec } from "../ExecContext";
import { useRepoMeta } from "../RepoMetaContext";
import { useFreshness } from "../useFreshness";
import { FreshnessBar, FreshnessCallout } from "../components/Freshness";
import { useRunState } from "../useRunState";
import { useJobStream } from "../useJobStream";
import { splitDiffSizes } from "../testPaths";

const LEVEL_ICON: Record<string, string> = { safe: "🛡️", caution: "⚠️", risk: "⛔", unknown: "❔" };
const SEV_TONE: Record<string, "red" | "yellow" | "muted"> = { critical: "red", high: "red", medium: "yellow", low: "muted" };

/** PR detail content — designed for the flyout. Verdict and primary action
 *  first, then the merge blockers, checks, summary context, and the diff at
 *  the bottom, so the decision surface is visible without scrolling. */
export function PRDetailContent({ pr: prNum }: { pr: number }) {
  const [pr, setPr] = useState<PD>();
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [diffLoading, setDiffLoading] = useState(true);
  // Which diff the Diff panel shows: the PR's own change, or the conflicted
  // merge state a "Resolve merge conflicts" attempt paused on (#46) — carried
  // by a refusal and by a parked agent resolution alike.
  const [diffMode, setDiffMode] = useState<"pr" | "merge" | "merge3">("pr");
  const [lineComment, setLineComment] = useState<{ file: string; line: number } | null>(null);
  // The Checks row a Summary chip last opened; a new `n` reopens it.
  const [openedCheck, setOpenedCheck] = useState<{ key: string; n: number } | null>(null);
  const [retriggering, setRetriggering] = useState<string | null>(null);
  const [clearingSecret, setClearingSecret] = useState(false);
  const [err, setErr] = useState<string>();
  const { botLogin, dryRun, reportResult, activeReviewers, pushToast } = useExec();
  const { meta } = useRepoMeta();
  // bump to re-fetch the per-PR action log after an action lands here.
  const [actLog, setActLog] = useState(0);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset the detail panes before fetching the selected PR
    setPr(undefined); setDiff(null); setDiffLoading(true); setDiffMode("pr"); setErr(undefined);
    api.pr(prNum).then(setPr).catch((e) => setErr(String(e)));
    api.diff(prNum).then((d) => { setDiff(d); setDiffLoading(false); }).catch(() => setDiffLoading(false));
  }, [prNum]);

  // Quiet re-fetch after an action lands (no loading flash): pulls the recomputed
  // suggestion/verdict/merge-gate so, e.g., a SECURITY re-run flips the card.
  // Returns its promise so callers that need the fresh state landed before
  // clearing a busy flag (queueVerify/dequeueVerify) can await it.
  const reloadPr = () => api.pr(prNum).then(setPr).catch(() => {});

  // Sandbox-verification queue: runner liveness for the panel's offline chip,
  // and queue/cancel actions on the shared store's verify_request.
  const [runner, setRunner] = useState<VerifyRunner | null>(null);
  // Which verify action is in flight, if any — drives both the disabled state
  // and an explicit "Queuing…"/"Cancelling…" label. Cleared only after the
  // re-fetched PR reflects the new verify_request status, so the button never
  // flashes back to its pre-click state while waiting on that re-fetch (#652).
  const [verifyBusy, setVerifyBusy] = useState<"queue" | "dequeue" | null>(null);
  useEffect(() => { api.verifyRunner().then(setRunner).catch(() => {}); }, [prNum]);
  const verifyInFlight = pr?.verify_request?.status === "queued" || pr?.verify_request?.status === "running"
    || pr?.verify_request?.status === "waiting-for-base";
  useEffect(() => {
    // an in-flight request resolves on the runner's side — poll it into view
    if (!verifyInFlight) return;
    const t = window.setInterval(() => {
      api.pr(prNum).then(setPr).catch(() => {});
      api.verifyRunner().then(setRunner).catch(() => {});
    }, 10000);
    return () => window.clearInterval(t);
  }, [verifyInFlight, prNum]);
  const queueVerify = async () => {
    if (verifyBusy) return;
    setVerifyBusy("queue");
    try { await api.queueVerify(prNum); } catch (e) { window.alert(String(e)); }
    await reloadPr();
    setVerifyBusy(null);
  };
  const dequeueVerify = async () => {
    if (verifyBusy) return;
    setVerifyBusy("dequeue");
    try { await api.dequeueVerify(prNum); } catch (e) { window.alert(String(e)); }
    await reloadPr();
    setVerifyBusy(null);
  };

  // Autofix queue: runner liveness (plus whether this deployment holds a push
  // identity at all, which is what disables the buttons) and the queue/cancel/
  // approve actions on the shared store's fix_request.
  const [fixRunner, setFixRunner] = useState<FixRunner | null>(null);
  const [fixBusy, setFixBusy] = useState<FixActionName | "dequeue" | "approve" | null>(null);
  useEffect(() => { api.fixRunner().then(setFixRunner).catch(() => {}); }, [prNum]);
  const fixStatus = pr?.fix_request?.status;
  const fixInFlight = fixStatus === "queued" || fixStatus === "running"
    || fixStatus === "approved" || fixStatus === "pushing";
  useEffect(() => {
    // an in-flight request resolves on the runner's side — poll it into view
    if (!fixInFlight) return;
    const t = window.setInterval(() => {
      api.pr(prNum).then(setPr).catch(() => {});
      api.fixRunner().then(setFixRunner).catch(() => {});
    }, 10000);
    return () => window.clearInterval(t);
  }, [fixInFlight, prNum]);
  const queueFix = async (action: FixActionName, guidance?: string) => {
    if (fixBusy) return;
    setFixBusy(action);
    try { await api.queueFix(prNum, action, guidance); } catch (e) { window.alert(String(e)); }
    await reloadPr();
    setFixBusy(null);
  };
  const dequeueFix = async () => {
    if (fixBusy) return;
    setFixBusy("dequeue");
    try { await api.dequeueFix(prNum); } catch (e) { window.alert(String(e)); }
    await reloadPr();
    setFixBusy(null);
  };
  const approveFix = async () => {
    if (fixBusy) return;
    setFixBusy("approve");
    try {
      const res = await api.approveFix(prNum, dryRun);
      if (res.status === "dry-run") {
        pushToast(`(dry run) #${prNum} · ${res.action ?? "fix"} push`, "yellow",
          { detail: res.detail });
      }
    } catch (e) { window.alert(String(e)); }
    await reloadPr();
    setFixBusy(null);
  };

  // Live freshness check (#25): re-fetch this PR's upstream state in the
  // background and warn loudly if it diverged from the snapshot we're showing.
  const fresh = useFreshness([prNum]);
  // The check persists what diverged into the store, so read the PR back to
  // bring the stored checks and gates up to what it found.
  useEffect(() => {
    if (fresh.status === "done" && fresh.changed) reloadPr();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fresh.status, fresh.changed, fresh.at]);

  // Re-run analysis right from the "new commits" banner (#582): reuses the
  // cluster-scoped triage-cluster job — CLUSTER/ANALYZE aren't single-PR
  // operations — since re-analysis for a PR is always its cluster's re-analysis.
  // Only offered once the PR has landed in a cluster; a PR ingested but not yet
  // clustered has no cluster job to run and needs the CLUSTER phase first.
  // The job (like the two below) runs to completion on the backend regardless
  // of this page's lifetime, and reattaches to one already in flight on mount
  // (#683) — navigating away and back shows it picking up where it left off.
  const analyzeJob = useJobStream("triage-cluster", { cluster: pr?.clusters[0] }, (status) => {
    pushToast(`#${prNum} · Re-analysis ${status}`, status === "done" ? "green" : "red",
      { detail: status === "done" ? "disposition + freshness reloaded below" : "the phase exited non-zero — see the log" });
    reloadPr();
    fresh.refresh();
  });
  const runAnalyze = () => {
    const cid = pr?.clusters[0];
    if (cid == null || analyzeJob.running) return;
    pushToast(`▶ #${prNum} · Re-analysis — running…`, "muted");
    analyzeJob.start(`/api/jobs/run/triage-cluster?cluster=${cid}`);
  };

  // Run-state (#10): has a live action already landed on this PR? Drives the
  // re-fire guard and the Reopen undo on the action controls.
  const run = useRunState([prNum]);
  const rs = run.byPr[prNum];

  // Re-trigger a reviewer by posting its mention as the configured bot — its
  // manual-review webhook re-runs against the current head, no new commit.
  const retriggerReview = async (reviewerId: string, label: string) => {
    if (retriggering) return;
    if (!dryRun && !window.confirm(`Re-trigger the ${label} review on #${prNum} as ${botLogin}?`)) return;
    setRetriggering(reviewerId);
    const r = await api.retriggerReview(prNum, reviewerId, dryRun);
    setRetriggering(null);
    reportResult(r);
  };

  // Re-run the SECURITY phase from the "Security review" check row
  // (#581) — streamed like the cluster-page re-run (ClusterDetail #56). On
  // done, reload so the fresh verdict flips the row (and the merge gate).
  const secJob = useJobStream("security-review", { pr: prNum }, (status) => {
    pushToast(`#${prNum} · Security review ${status}`, status === "done" ? "green" : "red",
      { detail: status === "done" ? "verdict reloaded below" : "the phase exited non-zero — see the log" });
    run.refresh(); setActLog((k) => k + 1); reloadPr();
  });
  const runSecurity = () => {
    if (secJob.running) return;
    // The same global toast path every other action uses (#67/#69), so a
    // long-running job is as visible as a merge/close/comment.
    pushToast(`▶ #${prNum} · Security review — running…`, "muted");
    secJob.start(`/api/jobs/run/security-review?pr=${prNum}`);
  };

  // Run the THREAT SCAN phase from the "Threat scan" check row,
  // scoped to just this PR (`threat_scan.py --only`) — same start-or-reattach
  // job pattern as the security re-run above; reattaches to one already
  // running if this page reloads (#683).
  const secretJob = useJobStream("threat-scan-pr", { pr: prNum }, (status) => {
    pushToast(`#${prNum} · Threat scan ${status}`, status === "done" ? "green" : "red",
      { detail: status === "done" ? "verdict reloaded below" : "the phase exited non-zero — see the log" });
    reloadPr();
  });
  const runSecretScan = () => {
    if (secretJob.running) return;
    pushToast(`▶ #${prNum} · Threat scan — running…`, "muted");
    secretJob.start(`/api/jobs/run/threat-scan-pr?pr=${prNum}`);
  };

  // Re-run the design review from its check row: the single-PR job replaces
  // whatever review the head already has.
  const designJob = useJobStream("design-review-pr", { pr: prNum }, (status) => {
    pushToast(`#${prNum} · Design review ${status}`, status === "done" ? "green" : "red",
      { detail: status === "done" ? "review reloaded below" : "the job exited non-zero — see the log" });
    reloadPr();
  });
  const runDesign = () => {
    if (designJob.running) return;
    pushToast(`▶ #${prNum} · Design review — running…`, "muted");
    designJob.start(`/api/jobs/run/design-review-pr?pr=${prNum}`);
  };

  // Record that the secret-leak finding the operator reviewed is no
  // credential: it lifts the merge block for that line at that head only.
  const clearSecretLeak = async (headSha: string, evidence: string) => {
    if (clearingSecret) return;
    if (!window.confirm(`Mark the flagged line in #${prNum} as not a real secret?\n\n${evidence}\n\n`
      + `This lifts the merge block for this line at commit ${headSha.slice(0, 7)} and dismisses its action item. `
      + "The threat scan checks any new push again.")) return;
    setClearingSecret(true);
    try {
      await api.clearSecretLeak(prNum, headSha);
      pushToast(`#${prNum} · marked not a secret`, "green");
      await reloadPr();
    } catch (e) {
      pushToast(`#${prNum} · couldn't mark it not a secret`, "red", { detail: String(e) });
    } finally {
      setClearingSecret(false);
    }
  };

  if (err) return <div className="error">Failed: {err}</div>;
  if (!pr) return <div className="muted pad">Loading #{prNum}…</div>;

  const findings = pr.security_detail?.findings ?? [];
  const securityHasRun = pr.checks?.checks.some((check) => check.key === "security" && check.at != null) ?? false;
  const secretsHasRun = pr.checks?.checks.some((check) => check.key === "secrets" && check.at != null) ?? false;
  const sum = pr.safety_summary;
  // The security banner shows only a problem: anything short of a GREEN at
  // this head, and a missing review only on a merge pick (the Summary's 🛡️ chip
  // carries the rest).
  const securityStale = pr.checks?.checks.some((check) => check.key === "security" && check.stale) ?? false;
  const securityProblem = !!sum && (sum.level === "safe" ? securityStale
    : sum.level !== "unknown" || pr.disposition === "merge");
  const sz = pr.size;
  const ci = pr.ci_checks ?? [];

  // The conflict diff a "Resolve merge conflicts" attempt captured while its
  // rebase was paused — the Diff panel's second mode (#46). Rides refusals and
  // parked agent resolutions alike.
  const mergeDiff = pr.fix_request?.result?.merge_diff ?? null;
  const conflictPaths = pr.fix_request?.result?.conflict_paths ?? [];

  // non-test surface area + test-removal flag (#17/#22): prefer the loaded diff
  // (always present here) split by the served profile's test-path rules, fall
  // back to the backend's cached-diff split until the RepoMeta fetch lands or
  // when the served patterns don't compile as JS RegExp.
  const split = (diff?.diff && meta ? splitDiffSizes(diff.diff, meta.test_paths) : null) ?? pr.size_split ?? null;

  // Ctrl/⌘-click a diff line → open it on GitHub at this PR's head SHA (#18).
  const repo = pr.url?.match(/github\.com\/([^/]+\/[^/]+)/)?.[1] ?? null;
  const blobUrl = (file: string, line: number): string | null =>
    pr.head_sha && repo ? `https://github.com/${repo}/blob/${pr.head_sha}/${file}#L${line}` : null;

  // A PR already merged/closed upstream: its triage surface (security verdict,
  // merge gate, disposition action) is moot — it was computed when the PR was an
  // open candidate. Show the current state and skip to the informational context.
  const resolved = pr.github_state === "merged" || pr.github_state === "closed";

  // #581: each check row in ChecksPanel carries its own re-run/queue action and
  // (when there's more to say than the one-line summary) its expanded detail —
  // right there next to the status, instead of scattered across separate panels.
  const checksActions: Partial<Record<string, ReactNode>> = {};
  const checksBodies: Partial<Record<string, ReactNode>> = {};
  const checks = withLiveConflicts(pr.checks, fresh.byPr[prNum]);
  const fixButton = (action: "update" | "rebase" | "describe") => (
    <FixActionButton action={action} req={pr.fix_request ?? null} runner={fixRunner} busy={fixBusy}
      resolved={resolved} onQueue={queueFix} />
  );
  const resolveConflicts = fixButton("rebase");
  if (!resolved) {
    checksActions.drift = fixButton("update");
    if (pr.signals?.ci === "unreported") checksActions.ci = fixButton("update");
  }
  if (checks?.checks.some((check) => check.key === "mergeable" && check.status === "fail")) {
    checksActions.mergeable = resolveConflicts;
  }

  if (!resolved) {
    const retriggerable = activeReviewers("review").filter((r) => r.retrigger);
    checksActions.review = (
      <>
        {fixButton("describe")}
        {retriggerable.map((r) => (
          <button key={r.id} className="btn-secondary sm" onClick={() => void retriggerReview(r.id, r.label)}
            disabled={retriggering !== null}
            title={`Re-trigger the ${r.label} review as ${botLogin} — no new commit needed`}>
            {retriggering === r.id ? "…" : retriggerable.length > 1 ? `↻ ${r.label}` : "↻ Re-trigger"}
          </button>
        ))}
      </>
    );
  }
  const reviewBlocks = reviewerBlocks(pr.reviews_detail ?? null, (d) => d.kind === "review" && d.id !== "design", blobUrl);
  if (reviewBlocks.length > 0) checksBodies.review = <>{reviewBlocks}</>;
  const designBlocks = reviewerBlocks(pr.reviews_detail ?? null, (d) => d.id === "design", blobUrl, { summaryFirst: true });
  if (designBlocks.length > 0 || designJob.log.length > 1) {
    checksBodies.design = (
      <>
        {designBlocks}
        {designJob.log.length > 1 && (
          <div className="joblog" aria-label="design review log">
            {designJob.log.map((l, i) => <div key={i} className="logline">{l}</div>)}
          </div>
        )}
      </>
    );
  }
  if (!resolved) {
    const design = pr.reviews_detail?.design ?? null;
    const goal = design && design.digest.stale !== true ? designFixGoal(design.entry?.findings ?? []) : "";
    const fixWhy = fixUnavailable(fixRunner, pr.fix_request ?? null, resolved);
    checksActions.design = (
      <>
        {goal && (
          <button className="btn-secondary sm" disabled={fixBusy != null || fixWhy != null}
            onClick={() => void queueFix("fix", goal)}
            title={fixWhy ?? "Queue an autofix aimed at these findings. The agent writes the change, "
              + "a second agent reviews it, and it parks in Autofix for your approval."}>
            {fixBusy === "fix" ? "Queuing…" : "🔧 Fix findings"}
          </button>
        )}
        <button className="btn-secondary sm" onClick={runDesign} disabled={designJob.running}
          title="Design-review this PR's current head now, replacing any review it has. Runs on a machine holding a verify base pin.">
          {designJob.running ? "Running…" : design?.entry ? "↻ Re-run" : "↻ Run"}
        </button>
      </>
    );
  }
  const scanBlocks = reviewerBlocks(pr.reviews_detail ?? null, (d) => d.kind === "scanner", blobUrl);
  if (scanBlocks.length > 0) checksBodies.scans = <>{scanBlocks}</>;

  if (ci.length > 0) {
    checksBodies.ci = ci.map((c, i) => (
      <div key={i} className="ci-row">
        <span className={c.conclusion === "success" ? "ok" : c.conclusion === "failure" ? "bad" : "muted"}>
          {c.conclusion === "success" ? "✓" : c.conclusion === "failure" ? "✗" : "•"}
        </span> {c.name} <span className="muted small">{c.conclusion}</span>
      </div>
    ));
  }

  if (!resolved) {
    checksActions.security = (
      <button className="btn-secondary sm" onClick={runSecurity} disabled={secJob.running}
        title="Run the 3-lens adversarial security review on this PR now.">
        {secJob.running ? "Running…" : securityHasRun ? "↻ Re-run" : "↻ Run"}
      </button>
    );
    checksActions.secrets = (
      <button className="btn-secondary sm" onClick={runSecretScan} disabled={secretJob.running}
        title="Scan this PR's diff for committed secrets and attack-pattern signatures now.">
        {secretJob.running ? "Running…" : secretsHasRun ? "↻ Re-run" : "↻ Run"}
      </button>
    );
  }
  if (secretJob.log.length > 1) {
    checksBodies.secrets = (
      <div className="joblog" aria-label="threat scan log">
        {secretJob.log.map((l, i) => <div key={i} className="logline">{l}</div>)}
      </div>
    );
  }
  if (findings.length > 0 || secJob.log.length > 1) {
    checksBodies.security = (
      <>
        {secJob.log.length > 1 && (
          <div className="joblog" aria-label="security review log" style={{ marginBottom: findings.length ? 8 : 0 }}>
            {secJob.log.map((l, i) => <div key={i} className="logline">{l}</div>)}
          </div>
        )}
        {findings.map((f, i) => (
          <Collapsible key={i} tone={SEV_TONE[f.severity?.toLowerCase() ?? ""] ?? "muted"}
            summary={<><b className="sev">{f.severity}</b> {f.title}</>}>
            <div className="finding-detail">{f.detail}</div>
            <div className="finding-loc">📍 {f.location} · <span className="muted">{f.lens}/{f.category}</span></div>
          </Collapsible>
        ))}
      </>
    );
  }

  // Analysis is cluster-scoped: re-running it re-analyzes every PR in the cluster.
  const analysisStage = pr.stages?.find((s) => s.key === "analysis");
  if (pr.clusters.length && analysisStage && analysisStage.status !== "pass") {
    checksActions.analysis = (
      <button className="btn-secondary sm" onClick={runAnalyze} disabled={analyzeJob.running}
        title={`Analyze cluster ${pr.clusters[0]} — every PR in it — against its current heads.`}>
        {analyzeJob.running ? "Analyzing…" : analysisStage.status === "na" ? "Analyze now" : "↻ Re-analyze"}
      </button>
    );
  }

  checksActions.verify = (
    <VerifyAction v={pr.verify_detail ?? null} req={pr.verify_request ?? null}
      busy={verifyBusy} canQueue={!resolved} onQueue={queueVerify} onDequeue={dequeueVerify} />
  );
  checksBodies.verify = (
    <VerifyBody v={pr.verify_detail ?? null} req={pr.verify_request ?? null} runner={runner} repo={repo} />
  );

  // A Summary chip opens its Checks row (or leads to the Checks panel or a diff
  // file); the scroll waits a frame for the opened row to render.
  const jump = (to: AnalysisTarget) => {
    if ("diff" in to) { jumpToDiff(to.diff, null); return; }
    const key = "check" in to ? to.check : null;
    if (key) setOpenedCheck((o) => ({ key, n: (o?.n ?? 0) + 1 }));
    window.requestAnimationFrame(() => {
      const el = document.getElementById(key ? checkRowId(key) : CHECKS_ID);
      if (el) flashTo(el);
    });
  };

  // The author's own description, collapsed under the Summary's facts.
  const description = pr.body && (
    <Collapsible className="summary-description" summary={<>Full PR description</>}>
      <div className="markdown prbody-md">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{pr.body.slice(0, 4000)}</ReactMarkdown>
      </div>
    </Collapsible>
  );

  // A close-dup's evidence: each change in the PR and where it already exists.
  const dupCoverage = Boolean(pr.concerns?.length) && (
    <div className="dup-coverage">
      <div className="muted small dup-coverage-title"
        title="Each change in this PR, and where that change already exists.">
        Duplicate coverage
      </div>
      <table className="facts-table">
        <thead><tr><th>Change</th><th>Covered by</th><th>Evidence</th></tr></thead>
        <tbody>
          {(pr.concerns ?? []).map((c, i) => (
            <tr key={i}>
              <td>
                {c.label}
                {Boolean(c.paths?.length) && (
                  <span className="muted small"> ({(c.paths ?? []).slice(0, 3).join(", ")}{(c.paths ?? []).length > 3 ? ", …" : ""})</span>
                )}
              </td>
              <td>
                <span className={`chip chip-${coverageTone(c.coverage)} sm`}
                  title={c.coverage === "landed" ? c.landed_sha : undefined}>
                  {c.coverage === "pr" && c.covered_by != null
                    ? <PRLink n={c.covered_by} />
                    : coverageLabel(c)}
                </span>
              </td>
              <td className="muted small">{c.evidence ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {Boolean(pr.sanity_trips?.length) && (
        <div className="callout dup-sanity">
          A person needs to review this because: {(pr.sanity_trips ?? []).join("; ")}.
        </div>
      )}
    </div>
  );

  return (
    <div className="prcontent">
      {/* header */}
      <div className="prc-head">
        <h2>#{pr.number} — {pr.title}</h2>
        <div className="metaline">
          {pr.author_stats?.url
            ? <a href={pr.author_stats.url} target="_blank" rel="noreferrer" title={authorTip(pr.author_stats)}>@{pr.author}</a>
            : <span title={authorTip(pr.author_stats)}>@{pr.author}</span>}
          {pr.trusted_author && <span className="chip chip-gold sm" title="A contributor we trust. When two PRs are about equally good, theirs is preferred.">trusted</span>}
          {pr.stacked && (pr.stacked_on != null
            ? <Link className="chip chip-muted" to={`/prs/${pr.stacked_on}`} title={`Stacked on #${pr.stacked_on} (base ${pr.base_ref}), not the default branch. It is never merge-ready and no hunter judges it until its base merges and GitHub retargets it.`}>stacked on #{pr.stacked_on}</Link>
            : <span className="chip chip-muted" title={`Targets ${pr.base_ref}, not the default branch. It is never merge-ready and no hunter judges it until GitHub retargets it.`}>stacked on {pr.base_ref}</span>)}
          {pr.clusters.map((cid) => <Link key={cid} className="chip chip-blue" to={`/prs/clusters/${cid}`} title="The group of PRs that fix the same problem. Click to open.">cluster {cid}</Link>)}
          {pr.url && <a className="chip chip-muted" href={pr.url} target="_blank" rel="noreferrer">GitHub ↗</a>}
          {resolved && <span className={`chip ${pr.github_state === "merged" ? "chip-purple" : "chip-muted"}`} title="Current state on GitHub">{pr.github_state}</span>}
          <span className="fresh-refreshed"><FreshnessBar f={fresh} /></span>
        </div>
      </div>

      {/* Live freshness (#25): loud, above everything — acting on a stale
          picture is how you contradict reality in a contributor's PR. */}
      <FreshnessCallout diverged={fresh.byPr[prNum]}
        onRerunAnalysis={pr.clusters.length ? runAnalyze : undefined}
        conflictAction={resolved ? undefined : resolveConflicts}
        rerunning={analyzeJob.running} rerunLog={analyzeJob.log} />

      {/* Verdict and primary action first, before anything scrolls. Deferred
          (dependency bump): the merge/security/action surface is all noise
          here — the pipeline doesn't triage these and the author lands them
          upstream. One banner stands in for the whole action stack; the
          informational tiles + diff below stay for context. */}
      {pr.out_of_scope ? (
        <>
        <div className="verdict-banner v-safe">
          <span className="vb-icon">📦</span>
          <div>
            <div className="vb-headline">Skipped — automatic library update</div>
            <div className="vb-detail">
              {pr.suggestion?.rationale ??
                "An automatic library update. We skip these; they're handled on GitHub."}
            </div>
          </div>
        </div>
        {description}
        </>
      ) : resolved ? (
        <>
        <div className={`verdict-banner ${pr.github_state === "merged" ? "v-safe" : ""}`}>
          <span className="vb-icon">{pr.github_state === "merged" ? "✅" : "🚫"}</span>
          <div>
            <div className="vb-headline">{pr.github_state === "merged" ? "Merged upstream" : "Closed upstream"}</div>
            <div className="vb-detail">
              This PR is already {pr.github_state} on GitHub, so there's nothing to do. The details below are from when it was still open.
            </div>
          </div>
        </div>
        <section className="suggestion summary-panel tone-muted">
          <h3 className="summary-title">Summary</h3>
          <PRStory pr={pr} onWritten={reloadPr} />
          {description}
        </section>
        {/* A malicious PR's evidence outlives the PR: closing it upstream is
            when GitHub may stop serving it. */}
        {pr.threat === "malicious" && (
          <div className="gate-block-callout">
            <div className="co-headline">⛔ Flagged malicious</div>
            <ThreatEvidencePanel prNum={prNum} refresh={pr} onCapture={runSecretScan} />
          </div>
        )}
        </>
      ) : (
       <>
      {/* Problems first, loud, above the Summary: the security verdict when it
          is anything but a current GREEN, then the merge blocks. */}
      {sum && securityProblem && (
        <div className={`verdict-banner ${sum.level === "risk" ? "v-risk" : "v-caution"}`}>
          <span className="vb-icon">{LEVEL_ICON[sum.level]}</span>
          <div>
            <div className="vb-headline">{sum.headline}</div>
            <div className="vb-detail">{sum.detail}</div>
            {findings.length > 0 && (() => {
              const { shown, more } = bannerFindings(findings);
              return (
                <ul className="vb-findings">
                  {shown.map((f, i) => (
                    <li key={i}>
                      <button type="button" className="link-btn" onClick={() => jump({ check: "security" })}
                        title={`${f.detail}\n📍 ${f.location}`}>
                        <b className="sev">{f.severity}</b> {f.title}
                      </button>
                    </li>
                  ))}
                  <li>
                    <button type="button" className="link-btn" onClick={() => jump({ check: "security" })}>
                      {more > 0 ? `+${more} more — see all in Checks ↓` : "Details in Checks ↓"}
                    </button>
                  </li>
                </ul>
              );
            })()}
          </div>
        </div>
      )}

      {/* CODEOWNERS manual-merge requirement (#15/#26) */}
      {pr.human_merge?.required && (
        <div className="codeowners-callout" title="These files are protected: a code owner has to approve and merge changes to them.">
          <div className="co-headline">⛔ A code owner must merge this — it changes protected files</div>
          <div className="co-detail">
            {botLogin} can't merge this. Ask a code owner to: <b>{pr.human_merge.owners.join(" + ")}</b>.
          </div>
          <ul className="co-paths">{pr.human_merge.paths.map((p) => <li key={p}><code>{p}</code></li>)}</ul>
        </div>
      )}

      {/* Threat gate (gates.pr_clean): a committed credential, a diff the scan
          could not read in full, or a malicious flag is a hard merge block —
          surface it loudly, like the CODEOWNERS one. */}
      {(() => {
        const blocks = (pr.clean_reasons || []).filter((r) => r.startsWith("secret-leak") || r.startsWith("unscannable-diff") || r.startsWith("malicious"));
        if (!blocks.length) return null;
        const malicious = blocks.some((r) => r.startsWith("malicious"));
        const secret = blocks.some((r) => r.startsWith("secret-leak"));
        const headline: string = malicious ? "⛔ Merge blocked — flagged malicious"
          : secret ? "🔑 Merge blocked — committed secret"
          : "⛔ Merge blocked — too big to scan for harmful code";
        const threat = pr.threat_detail;
        const evidence = threat?.detail?.["secret-leak"];
        const scannedHead = threat?.against_head_sha;
        return (
          <div className="gate-block-callout" title="A PR can't merge if it contains a password or key, looks harmful, or is too big to scan.">
            <div className="co-headline">{headline}</div>
            <div className="co-detail">
              {blocks.join("; ")}.{" "}
              {!malicious && secret && <>Ask the author to remove it (and replace the leaked key) with <b>Disposition → request changes</b>, or if it isn't a real secret, mark it so.</>}
              {!malicious && !secret && <>Ask the author to split it into smaller PRs with <b>Disposition → request changes</b>.</>}
            </div>
            {secret && evidence && <ul className="co-paths"><li><code>{evidence}</code></li></ul>}
            {!malicious && secret && evidence && scannedHead && !resolved && (
              <div style={{ marginTop: 8 }}>
                <button className="btn-secondary sm" disabled={clearingSecret}
                  onClick={() => void clearSecretLeak(scannedHead, evidence)}
                  title="You've checked the flagged line and it isn't a real password or key: lift this merge block for this commit and dismiss its action item. A new push is scanned again.">
                  {clearingSecret ? "Saving…" : "✓ Not a secret"}
                </button>
              </div>
            )}
            {malicious && <ThreatEvidencePanel prNum={prNum} refresh={pr} onCapture={runSecretScan} />}
          </div>
        );
      })()}

      {/* The Summary: what the PR fixes and how, what we found, and the one
          action surface under the suggestion. */}
      <PRActionBar pr={pr} runState={rs} onActed={() => { run.refresh(); setActLog((k) => k + 1); reloadPr(); }}
        summary={<><PRStory pr={pr} onWritten={reloadPr} />{description}{dupCoverage}<AnalysisLine pr={pr} onJump={jump} /></>} />
       </>
      )}

      {/* #550/#581: what checks ran, when, and what they found — plus the
          action that unblocks each one, right there. */}
      <ChecksPanel c={checks} stages={pr.stages} headSha={pr.live_head_sha ?? pr.head_sha}
        actions={checksActions} bodies={checksBodies} opened={openedCheck} />

      {/* Other actions: have the configured machine user push a change to the
          contributor's branch. Authored changes park their diff here for
          approval — nothing reaches GitHub until "Approve & push". The
          single-purpose actions also sit on the Checks rows they answer. */}
      <section className="panel">
        <div className="panel-head">
          <h3>Other actions</h3>
          <div className="panel-actions">
            <FixAction req={pr.fix_request ?? null} runner={fixRunner} busy={fixBusy}
              resolved={resolved} onQueue={queueFix} onDequeue={dequeueFix}
              onApprove={approveFix} />
          </div>
        </div>
        <FixBody req={pr.fix_request ?? null} runner={fixRunner} />
      </section>

      {/* #581: security findings, dynamic verification, review-provider
          feedback, and CI check runs now live as expandable detail inside
          their own rows in the Checks panel above, next to their action. */}

      {/* the PR's single timeline: GitHub's activity with our writes tagged,
          and a Prospector summary per commit, oldest first */}
      <PRHistory pr={pr.number} refresh={actLog} />

      {/* the diff, last — click a line for the explain/comment popup. When a
          "Resolve merge conflicts" attempt refused on conflicts, the captured
          conflict diff is offered as a second mode beside the PR's own change
          (#46). */}
      <section className="prc-section">
        <h3>Diff <span className="muted small" style={{ textTransform: "none", letterSpacing: 0 }}>· click a line to ask about it or comment · ⌘/ctrl-click to open it on GitHub</span></h3>
        <div className="diff-size">
          <span title="The whole change">
            <span className="add">+{sz?.additions ?? "?"}</span> <span className="del">−{sz?.deletions ?? "?"}</span>
            <span className="muted"> · {sz?.changed_files ?? "?"} files</span>
          </span>
          {split && (split.non_test.additions + split.non_test.deletions > 0 || split.test.files > 0) && (
            <span title="Changes to the app's code, not counting tests">
              <span className="muted">non-test</span>{" "}
              <span className="add">+{split.non_test.additions}</span> <span className="del">−{split.non_test.deletions}</span>
              <span className="muted"> · {split.non_test.files}f</span>
            </span>
          )}
          {split && split.test.files > 0 && (
            <span title="Changes to tests">
              <span className="muted">tests</span>{" "}
              <span className="add">+{split.test.additions}</span> <span className="del">−{split.test.deletions}</span>
              <span className="muted"> · {split.test.files}f</span>
            </span>
          )}
          {split?.removes_tests && (
            <span className="size-warn" title="This PR deletes more tests than it adds — check carefully">
              ⚠ removes more tests than it adds (−{split.test.deletions} / +{split.test.additions})
            </span>
          )}
        </div>
        {mergeDiff && (
          <div className="segmented diff-mode" role="tablist" aria-label="diff mode">
            <button className={diffMode === "pr" ? "on" : ""} role="tab" aria-selected={diffMode === "pr"}
              onClick={() => setDiffMode("pr")} title="The PR's own change.">
              Diff
            </button>
            <button className={diffMode === "merge" ? "on" : ""} role="tab" aria-selected={diffMode === "merge"}
              onClick={() => setDiffMode("merge")}
              title="The conflicted state a “Resolve merge conflicts” attempt paused on — the hunks before any resolution.">
              Merge diff
            </button>
            <button className={diffMode === "merge3" ? "on" : ""} role="tab" aria-selected={diffMode === "merge3"}
              onClick={() => setDiffMode("merge3")}
              title="The same conflict as three columns: main's code, the PR's code, and the merged result.">
              Merge diff side-by-side
            </button>
          </div>
        )}
        {diffMode !== "pr" && mergeDiff ? (
          <>
            <div className="diff-note">
              ⚠ Where this PR and the current base collide — the conflicted hunks a
              “Resolve merge conflicts” attempt paused on, before any resolution
              {conflictPaths.length > 0 && <> ({conflictPaths.length} file{conflictPaths.length === 1 ? "" : "s"})</>},
              not the PR's own change.
            </div>
            {diffMode === "merge3" ? <MergeSideBySide diffText={mergeDiff} /> : <DiffView diffText={mergeDiff} />}
          </>
        ) : (
          <>
            {diffLoading && <div className="diff-status"><span className="spinner" /> Loading diff…</div>}
            {!diffLoading && diff && (
              <>
                {diff.note && <div className="diff-note">ℹ️ {diff.note}</div>}
                {diff.diff
                  ? <DiffView diffText={diff.diff} onComment={(file, line) => setLineComment({ file, line })} blobUrl={blobUrl} anchored tiers={pr.risk_tier_files} />
                  : <div className="diff-status muted">{diff.error ? `No diff — ${diff.error}` : "No diff (empty/no commits)."}{pr.url && <> · <a href={pr.url} target="_blank" rel="noreferrer">GitHub ↗</a></>}</div>}
              </>
            )}
          </>
        )}
      </section>

      {lineComment && <LineCommentBox pr={pr.number} file={lineComment.file} line={lineComment.line} onClose={() => setLineComment(null)} />}
    </div>
  );
}


const STATUS_WORD: Record<ReviewDigest["status"], string> = {
  pass: "passed its bar", fail: "below its bar", stale: "stale — reviewed an earlier commit",
  pending: "awaiting its verdict", na: "not active",
};

function asCoverage(value: unknown): CoverageRecord | null {
  return value && typeof value === "object" && typeof (value as { status?: unknown }).status === "string"
    ? value as CoverageRecord : null;
}

/** The design lane's changed-line coverage: the share of the PR's changed
 *  source lines its related tests executed, and the lines they did not. */
function CoverageLine({ coverage }: { coverage: CoverageRecord }): ReactNode {
  let line: string;
  if (coverage.status === "measured") {
    line = coverage.executable > 0
      ? `${Math.round((100 * coverage.hit) / coverage.executable)}% of changed lines executed (${coverage.hit}/${coverage.executable})`
      : "no executable changed lines";
  } else if (coverage.status === "untested") {
    line = "no related tests reach the changed files";
  } else if (coverage.status === "fault") {
    return (
      <div className="muted small" style={{ marginTop: 8 }}>
        Coverage:{" "}
        <span className="chip chip-amber sm"
          title="This machine could not run the measurement; booked to the design lane's worker health, not to this PR">
          worker fault
        </span>{" "}
        could not run on this machine, not this PR: {coverage.reason ?? ""}
      </div>
    );
  } else {
    line = `${coverage.status === "skipped" ? "skipped" : "not measured"}: ${coverage.reason ?? ""}`;
  }
  const gaps = coverage.files.filter((f) => f.uncovered.length > 0 || !f.measured);
  return (
    <div className="muted small" style={{ marginTop: 8 }}>
      <span title="Measured by running the PR's related tests in the sandbox; reported by the PR's own test run, so advisory">
        Coverage: {line}
      </span>
      {coverage.tests.length > 0 && <div>tests run: {coverage.tests.map((t) => <code key={t} style={{ marginRight: 4 }}>{t}</code>)}</div>}
      {coverage.runs && (
        <div>
          {coverage.runs.passed} passed, {coverage.runs.failed} failed, {coverage.runs.skipped} skipped
          {coverage.runs.failed_files > 0 && ` · ${coverage.runs.failed_files} of ${coverage.runs.files} test files could not run in the sandbox`}
        </div>
      )}
      {gaps.length > 0 && (
        <ul style={{ margin: "2px 0 0 16px" }}>
          {gaps.map((f) => (
            <li key={f.path}>
              <code>{f.path}</code>{" "}
              {f.measured ? `lines ${f.uncovered.map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`)).join(", ")} not executed` : "not measured"}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** A finding's location: basename:line, full path in its tooltip. It scrolls
 *  to the line in the page's diff, or opens GitHub when the file is not there. */
function FindingLocation({ f, blobUrl }: {
  f: ReviewFinding;
  blobUrl: (file: string, line: number) => string | null;
}): ReactNode {
  if (!f.path) return null;
  const path = f.path;
  const label = `${path.split("/").pop()}${f.line != null ? `:${f.line}` : ""}`;
  const github = (f.line != null ? blobUrl(path, f.line) : null) ?? f.url;
  return (
    <div className="muted small finding-loc">
      (<a href={github ?? `#${diffFileId(path)}`} title={`${path}${f.line != null ? `:${f.line}` : ""}`}
        target={github ? "_blank" : undefined} rel={github ? "noreferrer" : undefined}
        onClick={(e) => { if (jumpToDiff(path, f.line)) e.preventDefault(); }}>
        <code>{label}</code>
      </a>)
    </div>
  );
}

/** One block per reviewer `which` picks on the PR page's Review / Design / Scans
 *  check rows: status line, stale banner, the reviewer's own summary (first
 *  with `summaryFirst`, else last), its open findings, and the design lane's
 *  coverage record last. */
function reviewerBlocks(detail: Record<string, { entry: ReviewEntry | null; digest: ReviewDigest }> | null,
                        which: (d: ReviewDigest) => boolean,
                        blobUrl: (file: string, line: number) => string | null,
                        { summaryFirst = false }: { summaryFirst?: boolean } = {}): ReactNode[] {
  if (!detail) return [];
  return Object.values(detail).filter((d) => which(d.digest) && d.digest.status !== "na").map(({ entry, digest }) => {
    const open = (entry?.findings ?? []).filter((f) => !f.resolved && !f.outdated);
    const trust = typeof digest.extra.trust_score === "number" ? digest.extra.trust_score : null;
    const verdict = typeof digest.extra.trust_verdict === "string" ? digest.extra.trust_verdict : null;
    const report = typeof digest.extra.report_url === "string" ? digest.extra.report_url : null;
    const coverage = asCoverage(digest.extra.coverage);
    return (
      <div key={digest.id} className="reviewer-block" style={{ marginBottom: 12 }}>
        <div>
          <b>{digest.label}</b>
          <span className={`chip chip-${digest.status === "pass" ? "green" : digest.status === "fail" ? "red" : "yellow"} sm`} style={{ marginLeft: 6 }}>
            {digest.summary_line}
          </span>
          {digest.advisory && <span className="chip chip-muted sm" style={{ marginLeft: 4 }} title="Shown for information; it does not block a merge">advisory</span>}
          <span className="muted small"> — {STATUS_WORD[digest.status]}{digest.reason && digest.status !== "pass" ? ` (${digest.reason})` : ""}</span>
        </div>
        {digest.stale === true && (
          <p className="muted small" style={{ margin: "4px 0" }}>
            ⚠ This verdict is from an older commit. The author may have pushed commits addressing the {digest.label} feedback since.
          </p>
        )}
        {summaryFirst && entry?.summary && (
          <div className="markdown reviewer-summary">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{`**Summary:** ${entry.summary}`}</ReactMarkdown>
          </div>
        )}
        {trust != null && <div className="muted small">Contributor trust {trust}/100{verdict ? ` · ${verdict}` : ""}</div>}
        {digest.checks.length > 0 && (
          <div className="muted small">
            {digest.checks.map((c, i) => (
              <div key={i}>{c.conclusion === "success" ? "✓" : c.conclusion === "neutral" ? "•" : c.status !== "completed" ? "…" : "✗"} {c.name}{c.title ? ` — ${c.title}` : ""}</div>
            ))}
          </div>
        )}
        {report && <div className="small"><a href={report} target="_blank" rel="noreferrer">full report ↗</a></div>}
        {open.length > 0 && (
          <div className="small" style={{ marginTop: 6 }}>
            <b>Findings:</b>
            <ul className="finding-list">
              {open.map((f, i) => {
                const verdict = f.verification?.verdict;
                return (
                  <li key={i}>
                    <span style={verdict === "refuted" ? { textDecoration: "line-through", opacity: 0.6 } : undefined}>
                      {f.title ?? f.body.split("\n")[0]}
                      {f.severity ? <span className="chip chip-muted sm" style={{ marginLeft: 6 }}>{f.severity}</span> : null}
                    </span>
                    {verdict && verdict !== "confirmed" && (
                      <span className="chip chip-muted sm" style={{ marginLeft: 4 }}
                            title={f.verification?.reason || undefined}>
                        {verdict === "unsure" ? `unsure (was ${f.claimed_severity ?? "major"})` : verdict}
                      </span>
                    )}
                    {f.url && <a className="action-log-link" href={f.url} target="_blank" rel="noreferrer" title="view on GitHub ↗"> ↗</a>}
                    <FindingLocation f={f} blobUrl={blobUrl} />
                    {verdict && verdict !== "confirmed" && f.verification?.reason && (
                      <div className="muted small">{f.verification.reason}</div>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        {!summaryFirst && entry?.summary && (
          <div className="markdown greptile-body" style={{ marginTop: 6 }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.summary}</ReactMarkdown>
          </div>
        )}
        {coverage && <CoverageLine coverage={coverage} />}
      </div>
    );
  });
}
