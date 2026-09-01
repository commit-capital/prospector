import { useState } from "react";
import { api, type IssueDisposition, type IssueExecResult, type IssueRow } from "../../api";
import { useExec } from "../../ExecContext";
import { BulkDialogFrame, BulkStatusChip } from "../shared/BulkRunDialog";
import { countStatuses, summaryLine } from "../shared/bulkSummary";

// An empty `comment` posts the server's default note on each issue.
export interface IssueClosePlan {
  disposition: IssueDisposition;
  comment: string;
  refs: Record<number, number>;
  skipped: number[];
}

const VERB: Record<IssueDisposition, string> = {
  "not-planned": "close as not planned",
  completed: "close as completed",
  fixed: "close as fixed",
  dup: "close as duplicate",
};

// Targets are snapshotted on open: the parent clears its selection when the
// run finishes.
export function IssueCloseConfirmDialog({ plan, selected: initialSelected, rows: initialRows, onResult, onClose, onDone }: {
  plan: IssueClosePlan;
  selected: number[];
  rows: IssueRow[];
  onResult: (n: number, res: IssueExecResult) => void;
  onClose: () => void;
  onDone: () => void;
}) {
  const { botLogin, dryRun } = useExec();
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<Record<number, IssueExecResult>>({});
  const [finished, setFinished] = useState(false);
  const [selected] = useState<number[]>(initialSelected);
  const [byNumber] = useState<Map<number, IssueRow>>(() => new Map(initialRows.map((r) => [r.number, r])));
  const { disposition, comment, refs, skipped } = plan;
  const skip = new Set(skipped);
  const takesRef = disposition === "fixed" || disposition === "dup";
  const refWord = disposition === "fixed" ? "fixed by" : "dup of";
  const toClose = selected.length - skipped.length;
  const submitted = Object.keys(results).length;

  const record = (n: number, res: IssueExecResult) => {
    setResults((r) => ({ ...r, [n]: res }));
    onResult(n, res);
  };

  const run = async () => {
    setRunning(true);
    try {
      for (const n of selected) {
        if (skip.has(n)) {
          record(n, { issue: n, action: "CLOSE_ISSUE", status: "skipped",
            detail: `no ${disposition === "fixed" ? "merged fixer PR" : "canonical issue"} on its row` });
          continue;
        }
        const body = {
          disposition, comment,
          ...(disposition === "fixed" ? { fixed_by: refs[n] } : {}),
          ...(disposition === "dup" ? { canonical: refs[n] } : {}),
        };
        try {
          record(n, await api.closeIssue(n, body, dryRun));
        } catch (e) {
          record(n, { issue: n, action: "CLOSE_ISSUE", status: "error", detail: String(e) });
        }
      }
    } finally {
      setRunning(false);
      setFinished(true);
      onDone();
    }
  };

  const summary = finished ? countStatuses(Object.values(results).map((r) => r.status)) : null;
  return (
    <BulkDialogFrame running={running} onClose={onClose}
      title={`${VERB[disposition]} — ${toClose} issue(s) ${dryRun ? "(dry run)" : `LIVE as ${botLogin}`}`}>
      {running && <div className="bulk-progress">Closing… {submitted} of {selected.length} submitted</div>}
      {comment
        ? <pre className="shared-comment">{comment}</pre>
        : takesRef && <div className="muted small bulk-perpr-note">📮 Each issue gets the default “{refWord} #…” note.</div>}
      <ul className="bulk-targets">
        {selected.map((n) => {
          const r = byNumber.get(n);
          const res = results[n];
          return (
            <li key={n} className={res ? "bulk-target-done" : undefined}>
              <a href={r?.url} target="_blank" rel="noreferrer" className="num">#{n}</a> {r?.title}
              {takesRef && (skip.has(n)
                ? <span className="chip chip-muted sm">skipped</span>
                : <span className="muted small">{refWord} #{refs[n]}</span>)}
              {res && <BulkStatusChip status={res.status} detail={res.detail} />}
            </li>
          );
        })}
      </ul>
      {summary
        ? <div className="bulk-summary">{summaryLine(summary)}</div>
        : <div className="modal-actions">
            <button className="btn-secondary" onClick={onClose} disabled={running}>Cancel</button>
            <button className={`btn-primary ${dryRun ? "" : "btn-live"}`} onClick={run} disabled={running || toClose === 0}>
              {running ? "Running…" : dryRun ? `Run (dry) ${toClose}` : `● Close ${toClose}`}
            </button>
          </div>}
      {summary && <div className="modal-actions"><button className="btn-primary" onClick={onClose}>Done</button></div>}
    </BulkDialogFrame>
  );
}
