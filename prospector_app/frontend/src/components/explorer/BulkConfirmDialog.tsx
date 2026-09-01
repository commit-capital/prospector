import { useEffect, useState } from "react";
import { api, type PRRow, type BulkResult } from "../../api";
import { useExec } from "../../ExecContext";
import { useJobGroup } from "../../useJobGroup";
import { GitHubPRLink } from "../GitHubPRLink";
import { BulkDialogFrame, BulkStatusChip } from "../shared/BulkRunDialog";
import { countStatuses, summaryLine } from "../shared/bulkSummary";
import type { BulkAction } from "./BulkActionBar";

export function BulkConfirmDialog({ action, comment, canonical, perPr = false, reviewer, selected, rows, onClose, onDone,
  onJobsFinished }:
  { action: BulkAction; comment: string; canonical?: number; perPr?: boolean; reviewer?: string; selected: number[];
    rows: PRRow[]; onClose: () => void; onDone: () => void; onJobsFinished?: () => void }) {
  const { botLogin, dryRun } = useExec();
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<Record<number, BulkResult>>({});
  const [summary, setSummary] = useState<Record<string, number> | null>(null);
  const securityJobs = useJobGroup(onJobsFinished);

  // `rows` is only the currently-loaded page, but `selected` can span every page
  // ("select all N matching"). Snapshot what's already on hand, then fetch the
  // rest by number so a multi-page selection acts on the whole thing, not just
  // whatever the grid happens to have loaded (#513). Snapshotting also keeps the
  // list and its per-PR result chips in place after the parent clears the
  // selection / refetches on done.
  const [targets, setTargets] = useState<PRRow[] | null>(() => {
    const byNum = new Map(rows.map((r) => [r.number, r]));
    return selected.every((n) => byNum.has(n))
      ? (selected.map((n) => byNum.get(n)) as PRRow[])
      : null;
  });
  useEffect(() => {
    if (targets !== null) return;
    let cancelled = false;
    api.queryPrs({ numbers: selected }, { limit: selected.length }).then((res) => {
      if (cancelled) return;
      const byNum = new Map(res.items.map((r) => [r.number, r]));
      setTargets(selected.map((n) => byNum.get(n)).filter(Boolean) as PRRow[]);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const postsComment = action !== "MERGE" && action !== "REVIEW_RETRIGGER"
    && action !== "QUEUE_VERIFY" && action !== "RUN_SECURITY";
  const localQueue = action === "QUEUE_VERIFY";
  const backgroundSecurity = action === "RUN_SECURITY";
  const localAction = localQueue || backgroundSecurity;
  const displayedResult = (result: BulkResult): BulkResult => {
    const job = securityJobs.byKey[result.pr];
    return job ? { ...result, status: job.status, detail: job.detail } : result;
  };
  const visibleSummary = backgroundSecurity && summary
    ? countStatuses(Object.values(results).map((r) => displayedResult(r).status))
    : summary;

  if (targets === null) {
    return (
      <BulkDialogFrame running={true} onClose={onClose} title={`${action} — ${selected.length} PR(s)`}>
        <div className="bulk-progress">Loading {selected.length} selected PR{selected.length === 1 ? "" : "s"}…</div>
      </BulkDialogFrame>
    );
  }

  const count = targets.length;
  const submittedCount = Object.keys(results).length;
  // Queueing for verification is a local store write, so the dry/live posting
  // mode does not apply to it.
  const blockedMerges = action === "MERGE"
    ? targets.filter((r) => r.merge_gate && !r.merge_gate.ok) : [];

  // Per-PR comments (#182): each PR's own suggested text. A PR with no suggestion
  // falls back server-side to the shared comment / default template.
  const perPrComments: Record<number, string> = {};
  if (perPr && postsComment) {
    for (const r of targets) {
      const c = r.suggestion?.bot_comment;
      if (c) perPrComments[r.number] = c;
    }
  }

  const run = async () => {
    setRunning(true);
    const startedJobs: Array<{ key: number; jobId: number }> = [];
    await api.executeBulk(
      { prs: targets.map((r) => r.number), action, comment: postsComment ? comment : undefined,
        comments: perPr && postsComment ? perPrComments : undefined,
        canonical, reviewer, dry_run: dryRun },
      (r) => {
        setResults((prev) => ({ ...prev, [r.pr]: r }));
        if (!backgroundSecurity || r.status !== "queued" || r.job_id === undefined) return;
        startedJobs.push({ key: r.pr, jobId: r.job_id });
      },
      (s) => { securityJobs.track(startedJobs); setSummary(s); setRunning(false); onDone(); },
    );
  };

  return (
    <BulkDialogFrame running={running} onClose={onClose}
      title={`${action} — ${count} PR(s) ${localQueue ? "(local verify queue)" : backgroundSecurity ? "(background jobs)" : dryRun ? "(dry run)" : `LIVE as ${botLogin}`}`}>
      {running && <div className="bulk-progress">Starting… {submittedCount} of {count} submitted</div>}
      {backgroundSecurity && summary && (
        <div className="bulk-progress">
          {securityJobs.jobCount === 0
            ? "No new security reviews were started"
            : securityJobs.allFinished
              ? `All ${securityJobs.jobCount} started security reviews finished`
              : `Security reviews… ${securityJobs.finishedCount} of ${securityJobs.jobCount} finished`}
        </div>
      )}
      {localQueue && (
        <div className="muted small bulk-perpr-note">
          🧪 Queues each PR for sandbox verification. The verify worker picks them up
          from the shared queue; nothing is posted upstream.
        </div>
      )}
      {backgroundSecurity && (
        <div className="muted small bulk-perpr-note">
          🛡️ Starts one deep security review per PR, with two reviews running at a
          time. The jobs continue if this dialog or page is closed.
        </div>
      )}
      {postsComment && (perPr
        ? <div className="muted small bulk-perpr-note">📮 Posting <b>each PR's own suggested comment</b> ({Object.keys(perPrComments).length} of {count} have one; the rest fall back to the default).</div>
        : comment && <pre className="shared-comment">{comment}</pre>)}
      {blockedMerges.length > 0 && (
        <div className="sug-verify">⛔ {blockedMerges.length} fail the merge gate and will be blocked (not merged):
          {" "}{blockedMerges.map((r) => `#${r.number}`).join(", ")}</div>
      )}
      <ul className="bulk-targets">
        {targets.map((r) => {
          const rawResult = results[r.number];
          const res = rawResult ? displayedResult(rawResult) : undefined;
          const gate = action === "MERGE" ? r.merge_gate : undefined;
          const resultFinished = res && (!backgroundSecurity || res.job_id === undefined
            || securityJobs.byKey[r.number]?.finished);
          return (
            <li key={r.number} className={resultFinished ? "bulk-target-done" : undefined}>
              <GitHubPRLink n={r.number} url={r.url} className="num" /> {r.title}
              {gate && !gate.ok && <span className="chip chip-red" title={gate.reason}>blocked</span>}
              {res && <BulkStatusChip status={res.status} detail={res.detail} />}
              {perPr && postsComment && (
                perPrComments[r.number]
                  ? <div className="muted small bulk-pr-comment" title={perPrComments[r.number]}>📮 {perPrComments[r.number].replace(/\s+/g, " ").slice(0, 80)}…</div>
                  : <div className="muted small bulk-pr-comment">📮 <i>default comment (no per-PR suggestion)</i></div>
              )}
            </li>
          );
        })}
      </ul>
      {visibleSummary
        ? <div className="bulk-summary">{summaryLine(visibleSummary)}</div>
        : <div className="modal-actions">
            <button className="btn-secondary" onClick={onClose} disabled={running}>Cancel</button>
            <button className={`btn-primary ${!dryRun && !localAction ? "btn-live" : ""}`} onClick={run} disabled={running}>
              {running ? "Running…" : localQueue ? `Queue ${count}` : backgroundSecurity ? `Start ${count}` : dryRun ? "Run (dry)" : `● Apply to ${count}`}
            </button>
          </div>}
      {summary && <div className="modal-actions"><button className="btn-primary" onClick={onClose}>
        {backgroundSecurity && securityJobs.jobCount > 0 && !securityJobs.allFinished ? "Close" : "Done"}
      </button></div>}
    </BulkDialogFrame>
  );
}
