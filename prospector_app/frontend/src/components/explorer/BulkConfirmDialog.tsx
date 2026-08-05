import { useEffect, useState } from "react";
import { api, type PRRow, type BulkResult } from "../../api";
import { useExec } from "../../ExecContext";
import { GitHubPRLink } from "../GitHubPRLink";
import type { BulkAction } from "./BulkActionBar";

export function BulkConfirmDialog({ action, comment, canonical, perPr = false, selected, rows, onClose, onDone }:
  { action: BulkAction; comment: string; canonical?: number; perPr?: boolean; selected: number[];
    rows: PRRow[]; onClose: () => void; onDone: () => void }) {
  const { botLogin, dryRun } = useExec();
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<Record<number, BulkResult>>({});
  const [summary, setSummary] = useState<Record<string, number> | null>(null);

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

  if (targets === null) {
    return (
      <div className="modal-backdrop">
        <div className="modal bulk-confirm" onClick={(e) => e.stopPropagation()}>
          <h3>{action} — {selected.length} PR(s)</h3>
          <div className="bulk-progress">Loading {selected.length} selected PR{selected.length === 1 ? "" : "s"}…</div>
        </div>
      </div>
    );
  }

  const count = targets.length;
  const doneCount = Object.keys(results).length;
  const postsComment = action !== "MERGE" && action !== "GREPTILE_RETRIGGER";
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
    await api.executeBulk(
      { prs: targets.map((r) => r.number), action, comment: postsComment ? comment : undefined,
        comments: perPr && postsComment ? perPrComments : undefined,
        canonical, dry_run: dryRun },
      (r) => setResults((prev) => ({ ...prev, [r.pr]: r })),
      (s) => { setSummary(s); setRunning(false); onDone(); },
    );
  };

  // While running, the backdrop click is a no-op — losing the dialog mid-run
  // leaves no way to tell whether it's still going or already finished.
  return (
    <div className="modal-backdrop" onClick={() => { if (!running) onClose(); }}>
      <div className="modal bulk-confirm" onClick={(e) => e.stopPropagation()}>
        <h3>{action} — {count} PR(s) {dryRun ? "(dry run)" : `LIVE as ${botLogin}`}</h3>
        {running && <div className="bulk-progress">Running… {doneCount} of {count} done</div>}
        {postsComment && (perPr
          ? <div className="muted small bulk-perpr-note">📮 Posting <b>each PR's own suggested comment</b> ({Object.keys(perPrComments).length} of {count} have one; the rest fall back to the default).</div>
          : comment && <pre className="shared-comment">{comment}</pre>)}
        {blockedMerges.length > 0 && (
          <div className="sug-verify">⛔ {blockedMerges.length} fail the merge gate and will be blocked (not merged):
            {" "}{blockedMerges.map((r) => `#${r.number}`).join(", ")}</div>
        )}
        <ul className="bulk-targets">
          {targets.map((r) => {
            const res = results[r.number];
            const gate = action === "MERGE" ? r.merge_gate : undefined;
            return (
              <li key={r.number} className={res ? "bulk-target-done" : undefined}>
                <GitHubPRLink n={r.number} url={r.url} className="num" /> {r.title}
                {gate && !gate.ok && <span className="chip chip-red" title={gate.reason}>blocked</span>}
                {res && <span className={`chip chip-${res.status === "executed" || res.status === "merged" ? "green" : res.status === "error" || res.status === "blocked" ? "red" : "muted"}`}>{res.status}</span>}
                {perPr && postsComment && (
                  perPrComments[r.number]
                    ? <div className="muted small bulk-pr-comment" title={perPrComments[r.number]}>📮 {perPrComments[r.number].replace(/\s+/g, " ").slice(0, 80)}…</div>
                    : <div className="muted small bulk-pr-comment">📮 <i>default comment (no per-PR suggestion)</i></div>
                )}
              </li>
            );
          })}
        </ul>
        {summary
          ? <div className="bulk-summary">{Object.entries(summary).map(([k, v]) => `${v} ${k}`).join(" · ")}</div>
          : <div className="modal-actions">
              <button className="btn-secondary" onClick={onClose} disabled={running}>Cancel</button>
              <button className={`btn-primary ${!dryRun ? "btn-live" : ""}`} onClick={run} disabled={running}>
                {running ? "Running…" : dryRun ? "Run (dry)" : `● Apply to ${count}`}
              </button>
            </div>}
        {summary && <div className="modal-actions"><button className="btn-primary" onClick={onClose}>Done</button></div>}
      </div>
    </div>
  );
}
