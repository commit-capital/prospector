import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type PRDetail as PD, type DiffResult, type VerifyRunner, type FixAction as FixActionName, type FixRunner, type ReviewDigest, type ReviewEntry } from "../api";
import { DriftChip, TierChip, authorTip } from "../components/Chips";
import { InfoTip } from "../components/InfoTip";
import { dispositionEntry } from "../glossary";
import { ChecksPanel } from "../components/ChecksPanel";
import { DiffView } from "../components/DiffView";
import { Collapsible } from "../components/Collapsible";
import { PRActionBar } from "../components/PRActionBar";
import { VerifyAction, VerifyBody } from "../components/VerifyPanel";
import { FixAction, FixBody } from "../components/FixPanel";
import { PRActionLog } from "../components/PRActionLog";
import { PRHistory } from "../components/PRHistory";
import { LineCommentBox } from "../components/ReviewModal";
import { useExec } from "../ExecContext";
import { useRepoMeta } from "../RepoMetaContext";
import { useFreshness } from "../useFreshness";
import { FreshnessBar, FreshnessCallout } from "../components/Freshness";
import { FactFreshnessPanel } from "../components/FactFreshness";
import { useRunState } from "../useRunState";
import { useJobStream } from "../useJobStream";
import { LinkedIssues } from "../components/LinkedIssues";
import { splitDiffSizes } from "../testPaths";

const LEVEL_ICON: Record<string, string> = { safe: "🛡️", caution: "⚠️", risk: "⛔", unknown: "❔" };
const SEV_TONE: Record<string, "red" | "yellow" | "muted"> = { critical: "red", high: "red", medium: "yellow", low: "muted" };
const DISPO_ICON: Record<string, string> = {
  "merge": "✅", "request-changes": "✋", "close-dup": "🗑️", "close-fixed": "🗑️", "close-stale": "🗑️", "needs-human": "👤",
};

/** PR detail content — designed for the flyout. Security/safety first, then
 *  size + checks, then concerns/greptile/ci, then the diff at the bottom. */
export function PRDetailContent({ pr: prNum }: { pr: number }) {
  const [pr, setPr] = useState<PD>();
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [diffLoading, setDiffLoading] = useState(true);
  // Which diff the Diff panel shows: the PR's own change, or the conflicted
  // merge state a "Resolve merge conflicts" attempt paused on (#46) — carried
  // by a refusal and by a parked agent resolution alike.
  const [diffMode, setDiffMode] = useState<"pr" | "merge">("pr");
  const [lineComment, setLineComment] = useState<{ file: string; line: number } | null>(null);
  const [retriggering, setRetriggering] = useState<string | null>(null);
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
  // "already closed" badge + re-fire guard on the action controls.
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

  // Re-run the SECURITY phase from the "Deep security review" check row
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

  // Run the THREAT SCAN phase from the "No committed secrets" check row,
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

  if (err) return <div className="error">Failed: {err}</div>;
  if (!pr) return <div className="muted pad">Loading #{prNum}…</div>;

  const findings = pr.security_detail?.findings ?? [];
  const securityHasRun = pr.checks?.checks.some((check) => check.key === "security" && check.at != null) ?? false;
  const secretsHasRun = pr.checks?.checks.some((check) => check.key === "secrets" && check.at != null) ?? false;
  const sum = pr.safety_summary;
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

  const retriggerable = activeReviewers("review").filter((r) => r.retrigger);
  if (retriggerable.length > 0 && !resolved) {
    checksActions.review = (
      <>
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
  const reviewBlocks = reviewerBlocks(pr.reviews_detail ?? null, "review");
  if (reviewBlocks.length > 0) checksBodies.review = <>{reviewBlocks}</>;
  const scanBlocks = reviewerBlocks(pr.reviews_detail ?? null, "scanner");
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

  checksActions.verify = (
    <VerifyAction v={pr.verify_detail ?? null} req={pr.verify_request ?? null}
      busy={verifyBusy} canQueue={!resolved} onQueue={queueVerify} onDequeue={dequeueVerify} />
  );
  checksBodies.verify = (
    <VerifyBody v={pr.verify_detail ?? null} req={pr.verify_request ?? null} runner={runner} repo={repo} />
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
          {pr.trusted_author && <span className="chip chip-gold sm" title="Trusted contributor — named by the repository profile; in ANALYZE their PR breaks an otherwise-close canonical tie (a tiebreaker, never an override of a clearly-better PR).">trusted</span>}
          <DriftChip s={pr.drift_state} />
          <TierChip tier={pr.risk_tier} pinnedBy={pr.risk_tier_paths} />
          {pr.clusters.map((cid) => <Link key={cid} className="chip chip-blue" to={`/clusters/${cid}`} title="A dedup cluster this PR belongs to — a group of PRs fixing the same root issue. Click to open.">cluster {cid}</Link>)}
          {pr.url && <a className="chip chip-muted" href={pr.url} target="_blank" rel="noreferrer">GitHub ↗</a>}
          {resolved && <span className={`chip ${pr.github_state === "merged" ? "chip-purple" : "chip-muted"}`} title="Current state on GitHub">{pr.github_state}</span>}
          <span className="fresh-refreshed"><FreshnessBar f={fresh} /></span>
        </div>
      </div>

      {/* Live freshness (#25): loud, above everything — acting on a stale
          picture is how you contradict reality in a contributor's PR. */}
      <FreshnessCallout diverged={fresh.byPr[prNum]}
        onRerunAnalysis={pr.clusters.length ? runAnalyze : undefined}
        rerunning={analyzeJob.running} rerunLog={analyzeJob.log} />

      {/* When each fact was computed and which commit it describes — so a
          recommendation can be dated before anyone acts on it. */}
      <FactFreshnessPanel facts={pr.fact_freshness} headSha={pr.head_sha}
        liveHeadSha={pr.live_head_sha} />

      <section className="prc-section">
        <h3>Issues this may fix</h3>
        <LinkedIssues issues={pr.issues} />
      </section>

      {/* #550/#581: what checks ran, when, and what they found — plus the
          action that unblocks each one, right there — the topline item,
          above even the recommended Action section below. */}
      <ChecksPanel c={pr.checks} actions={checksActions} bodies={checksBodies} />

      {/* Autofix: have the configured machine user push a small fix to the
          contributor's branch, rather than asking the author and waiting.
          Content-authoring actions park their diff here for approval — nothing
          reaches GitHub until "Approve & push". */}
      <section className="panel">
        <div className="panel-head">
          <h3>Autofix</h3>
          <div className="panel-actions">
            <FixAction req={pr.fix_request ?? null} runner={fixRunner} busy={fixBusy}
              resolved={resolved} onQueue={queueFix} onDequeue={dequeueFix}
              onApprove={approveFix} />
          </div>
        </div>
        <FixBody req={pr.fix_request ?? null} runner={fixRunner} />
      </section>

      {/* Deferred (dependency bump): the merge/security/action surface is all
          noise here — the pipeline doesn't triage these and the author lands
          them upstream. Replace the whole action stack with one banner; keep the
          informational tiles + diff below for context. */}
      {pr.out_of_scope ? (
        <div className="verdict-banner v-safe">
          <span className="vb-icon">📦</span>
          <div>
            <div className="vb-headline">Deferred — dependency bump, handled upstream</div>
            <div className="vb-detail">
              {pr.suggestion?.rationale ??
                "Dependabot dependency bump. The pipeline defers these; Socket reviews the package on the PR. No cluster, disposition, or triage action here."}
            </div>
          </div>
        </div>
      ) : resolved ? (
        <div className={`verdict-banner ${pr.github_state === "merged" ? "v-safe" : ""}`}>
          <span className="vb-icon">{pr.github_state === "merged" ? "✅" : "🚫"}</span>
          <div>
            <div className="vb-headline">{pr.github_state === "merged" ? "Merged upstream" : "Closed upstream"}</div>
            <div className="vb-detail">
              This PR is already {pr.github_state} on GitHub — no triage action applies. The size, checks, and diff below reflect its last review as an open PR.
            </div>
          </div>
        </div>
      ) : (
       <>
      {/* 1. SECURITY / SAFETY verdict — first and foremost */}
      {sum && (
        <div className={`verdict-banner v-${sum.level}`}>
          <span className="vb-icon">{LEVEL_ICON[sum.level]}</span>
          <div>
            <div className="vb-headline">{sum.headline}</div>
            <div className="vb-detail">{sum.detail}</div>
          </div>
        </div>
      )}

      {/* CODEOWNERS manual-merge requirement — loud, above the suggestion (#15/#26) */}
      {pr.human_merge?.required && (
        <div className="codeowners-callout" title="The upstream repo's branch ruleset requires a code owner to approve/merge these paths.">
          <div className="co-headline">⛔ Requires human merge — touches CODEOWNERS-gated code</div>
          <div className="co-detail">
            {botLogin} can't auto-merge this. A code owner must merge it: <b>{pr.human_merge.owners.join(" + ")}</b>.
          </div>
          <ul className="co-paths">{pr.human_merge.paths.map((p) => <li key={p}><code>{p}</code></li>)}</ul>
        </div>
      )}

      {/* Threat gate (gates.pr_clean): a committed credential or malicious flag
          is a hard merge block — surface it loudly, like the CODEOWNERS one. */}
      {(() => {
        const blocks = (pr.clean_reasons || []).filter((r) => r.startsWith("secret-leak") || r.startsWith("malicious"));
        if (!blocks.length) return null;
        const malicious = blocks.some((r) => r.startsWith("malicious"));
        return (
          <div className="gate-block-callout" title="gates.pr_clean refuses to merge a PR with a committed credential or a malicious flag.">
            <div className="co-headline">{malicious ? "⛔ Merge blocked — flagged malicious" : "🔑 Merge blocked — committed secret"}</div>
            <div className="co-detail">
              {blocks.join("; ")}.{" "}
              {!malicious && <>Remove it, rotate the key, and bounce the PR via <b>Disposition → request changes</b>.</>}
            </div>
          </div>
        );
      })()}

      {/* the one action surface — comment / approve / request-changes / merge /
          close / reopen, with the agent's recommendation folded in. */}
      <section className="prc-section">
        <h3>Action</h3>
        {pr.disposition && (() => {
          const entry = dispositionEntry(pr.disposition);
          const attn = pr.disposition === "needs-human";
          return (
            <div className={`dispo-banner ${attn ? "dispo-attn" : ""}`}>
              <span className="dispo-ico" aria-hidden="true">{DISPO_ICON[pr.disposition] ?? "•"}</span>
              <div>
                <div className="dispo-head">
                  <InfoTip entry={entry}>{entry?.title ?? pr.disposition}</InfoTip>
                </div>
                {pr.proposed_action?.rationale
                  ? <div className="dispo-why">{pr.proposed_action.rationale}</div>
                  : entry?.triggers && <div className="dispo-why muted">{entry.triggers}</div>}
                {pr.proposed_action?.fresh === false && (
                  <div className="dispo-stale">⟳ This analysis is stale — the PR head moved since it ran.</div>
                )}
              </div>
            </div>
          );
        })()}
        <PRActionLog pr={pr.number} refresh={actLog} />
        <PRActionBar pr={pr} runState={rs} onActed={() => { run.refresh(); setActLog((k) => k + 1); reloadPr(); }} />
      </section>
       </>
      )}

      {/* 2. at-a-glance: size */}
      <div className="stat-tiles">
        <div className="tile">
          <div className="tile-label">Change size</div>
          <div className="tile-val">
            <span className="add">+{sz?.additions ?? "?"}</span> <span className="del">−{sz?.deletions ?? "?"}</span>
            <span className="muted small"> · {sz?.changed_files ?? "?"} files</span>
          </div>
          {split && (split.non_test.additions + split.non_test.deletions > 0 || split.test.files > 0) && (
            <div className="size-split">
              <div title="Change excluding test files — the real source-code surface area">
                <span className="muted">non-test</span>{" "}
                <span className="add">+{split.non_test.additions}</span> <span className="del">−{split.non_test.deletions}</span>
                <span className="muted small"> · {split.non_test.files}f</span>
              </div>
              {split.test.files > 0 && (
                <div title="Test-file changes">
                  <span className="muted">tests</span>{" "}
                  <span className="add">+{split.test.additions}</span> <span className="del">−{split.test.deletions}</span>
                  <span className="muted small"> · {split.test.files}f</span>
                </div>
              )}
            </div>
          )}
          {split?.removes_tests && (
            <div className="size-warn" title="This PR removes more test code than it adds — review carefully">
              ⚠ removes more tests than it adds (−{split.test.deletions} / +{split.test.additions})
            </div>
          )}
        </div>
      </div>

      {/* #581: security findings, dynamic verification, review-provider
          feedback, and CI check runs now live as expandable detail inside
          their own rows in the Checks panel above, next to their action. */}

      {/* 3. agent summary — diff-grounded one-liner + mechanism, distinct from
          the submitter's PR description below. */}
      {pr.summary?.one_liner && (
        <section className="prc-section">
          <Collapsible summary={<>🧭 Agent summary — <span className="muted small" style={{ textTransform: "none", letterSpacing: 0 }}>{pr.summary.one_liner}</span></>}>
            {pr.summary.mechanism
              ? <div className="prbody">{pr.summary.mechanism}</div>
              : <div className="muted small">No mechanism detail.</div>}
          </Collapsible>
        </section>
      )}

      {/* 4. description */}
      {pr.body && (
        <section className="prc-section">
          <Collapsible summary={<>📝 PR description</>}>
            <div className="markdown prbody-md">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{pr.body.slice(0, 4000)}</ReactMarkdown>
            </div>
          </Collapsible>
        </section>
      )}

      {/* 5. condensed upstream history — comments, reviews, commits, and
          reopen/close/force-push/rename events, oldest first */}
      <PRHistory pr={pr.number} />

      {/* 6. the diff, last — click a line for the explain/comment popup. When a
          "Resolve merge conflicts" attempt refused on conflicts, the captured
          conflict diff is offered as a second mode beside the PR's own change
          (#46). */}
      <section className="prc-section">
        <h3>Diff <span className="muted small" style={{ textTransform: "none", letterSpacing: 0 }}>· click a line to explain or comment · ⌘/ctrl-click to open it on GitHub</span></h3>
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
          </div>
        )}
        {diffMode === "merge" && mergeDiff ? (
          <>
            <div className="diff-note">
              ⚠ Where this PR and the current base collide — the conflicted hunks a
              “Resolve merge conflicts” attempt paused on, before any resolution
              {conflictPaths.length > 0 && <> ({conflictPaths.length} file{conflictPaths.length === 1 ? "" : "s"})</>},
              not the PR's own change.
            </div>
            <DiffView diffText={mergeDiff} />
          </>
        ) : (
          <>
            {diffLoading && <div className="diff-status"><span className="spinner" /> Loading diff…</div>}
            {!diffLoading && diff && (
              <>
                {diff.note && <div className="diff-note">ℹ️ {diff.note}</div>}
                {diff.diff
                  ? <DiffView diffText={diff.diff} onComment={(file, line) => setLineComment({ file, line })} blobUrl={blobUrl} />
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

/** One block per reviewer of `kind` on the PR page's Review / Scans check rows:
 *  status line, stale banner, the bot's own summary, and its open findings. */
function reviewerBlocks(detail: Record<string, { entry: ReviewEntry | null; digest: ReviewDigest }> | null,
                        kind: ReviewDigest["kind"]): ReactNode[] {
  if (!detail) return [];
  return Object.values(detail).filter((d) => d.digest.kind === kind && d.digest.status !== "na").map(({ entry, digest }) => {
    const open = (entry?.findings ?? []).filter((f) => !f.resolved && !f.outdated);
    const trust = typeof digest.extra.trust_score === "number" ? digest.extra.trust_score : null;
    const verdict = typeof digest.extra.trust_verdict === "string" ? digest.extra.trust_verdict : null;
    const report = typeof digest.extra.report_url === "string" ? digest.extra.report_url : null;
    return (
      <div key={digest.id} className="reviewer-block" style={{ marginBottom: 12 }}>
        <div>
          <b>{digest.label}</b>
          <span className={`chip chip-${digest.status === "pass" ? "green" : digest.status === "fail" ? "red" : "yellow"} sm`} style={{ marginLeft: 6 }}>
            {digest.summary_line}
          </span>
          <span className="muted small"> — {STATUS_WORD[digest.status]}{digest.reason && digest.status !== "pass" ? ` (${digest.reason})` : ""}</span>
        </div>
        {digest.stale === true && (
          <p className="muted small" style={{ margin: "4px 0" }}>
            ⚠ This verdict is from an older commit. The author may have pushed commits addressing the {digest.label} feedback since.
          </p>
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
          <ul className="small" style={{ margin: "6px 0 0 16px" }}>
            {open.map((f, i) => (
              <li key={i}>
                {f.path ? <code>{f.path}{f.line != null ? `:${f.line}` : ""}</code> : null}
                {f.severity ? <span className="chip chip-muted sm" style={{ marginLeft: 4 }}>{f.severity}</span> : null}
                {" "}{f.title ?? f.body.split("\n")[0]}
                {f.url && <a className="action-log-link" href={f.url} target="_blank" rel="noreferrer" title="view on GitHub ↗"> ↗</a>}
              </li>
            ))}
          </ul>
        )}
        {entry?.summary && (
          <div className="markdown greptile-body" style={{ marginTop: 6 }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.summary}</ReactMarkdown>
          </div>
        )}
      </div>
    );
  });
}
