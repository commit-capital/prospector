import { useEffect, useMemo, useState } from "react";
import { api, type IssueDetail, type IssueExecResult } from "../api";
import { useExec } from "../ExecContext";
import { CommentEditor } from "./CommentEditor";
import { landed, ExecResultChip } from "./execResult";

type IAct = "already-fixed" | "dup" | "not-planned" | "completed" | "comment";

const OPTS: { v: IAct; label: string }[] = [
  { v: "already-fixed", label: "close — already-fixed" },
  { v: "dup", label: "close — dup of" },
  { v: "not-planned", label: "close — not planned" },
  { v: "completed", label: "close — completed" },
  { v: "comment", label: "comment" },
];

// The disposition's recommended action, pre-selected like PRActionBar does.
function recommended(d: IssueDetail): IAct {
  if (d.disposition === "close-fixed" && d.analysis?.fixed_by) return "already-fixed";
  if (d.disposition === "close-dup") return "dup";
  return "comment";
}

// Default comment for the chosen action. The fixed/dup templates come from the
// backend (d.fixed_comment / d.dup_comment — the same fixed_issue_comment /
// dup_issue_comment the executor posts); the request-repro seed is rendered
// from the issue's own asks.
function defaultComment(a: IAct, d: IssueDetail, canonical: string, fixer: string): string {
  // d.fixed_comment / d.dup_comment cite the analyzed fixer/canonical; only
  // prefill them while the # is unchanged, else leave empty so the backend
  // renders the template for the actual target.
  if (a === "already-fixed") return Number(fixer) === d.analysis?.fixed_by ? (d.fixed_comment ?? "") : "";
  if (a === "dup") return Number(canonical) === d.analysis?.canonical ? (d.dup_comment ?? "") : "";
  if (a === "comment" && d.disposition === "request-repro" && d.analysis?.asks?.length)
    return `Thanks for the report! To help us reproduce this, could you share:\n${d.analysis.asks.map((x) => `- ${x}`).join("\n")}`;
  return "";
}

/** One action surface for the issue detail flyout: close (already-fixed / dup /
 *  not-planned / completed) / comment, plus reopen. The stored triage verdict
 *  (d.disposition) picks the pre-selected action; the fixed/dup default comments
 *  come from the backend so the same wording the executor posts is previewed. */
export function IssueActionBar({ d, onActed }: { d: IssueDetail; onActed?: () => void }) {
  const { botLogin, dryRun, reportResult } = useExec();
  const [action, setAction] = useState<IAct>(() => recommended(d));
  const [canonical, setCanonical] = useState(() => (d.analysis?.canonical ? String(d.analysis.canonical) : ""));
  const [fixerInput, setFixerInput] = useState(() => (d.analysis?.fixed_by ? String(d.analysis.fixed_by) : ""));
  const [edited, setEdited] = useState<string | null>(null);
  const [openComment, setOpenComment] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<IssueExecResult | null>(null);

  const defComment = useMemo(() => defaultComment(action, d, canonical, fixerInput), [action, d, canonical, fixerInput]);
  const commentText = edited ?? defComment;
  const isEdited = edited != null && edited !== defComment;
  const fixer = fixerInput ? Number(fixerInput) : null;

  // comment / manual closes need a body; dup needs a #; already-fixed needs a fixer.
  const needsComment = (action === "comment" || action === "not-planned" || action === "completed") && !commentText.trim();
  const needsCanon = action === "dup" && !canonical;
  const needsFixer = action === "already-fixed" && !fixer;
  const blocked = needsComment || needsCanon || needsFixer;

  useEffect(() => { setEdited(null); }, [action, canonical, fixerInput]); // eslint-disable-line react-hooks/set-state-in-effect -- edit was for the old wording
  useEffect(() => { setResult(null); }, [dryRun]); // eslint-disable-line react-hooks/set-state-in-effect -- clear stale chip on mode flip
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- open the editor when the action requires a body
    if (action === "comment" || action === "not-planned" || action === "completed") setOpenComment(true);
  }, [action]);

  const fire = async () => {
    if (blocked) return;
    if (!dryRun && !window.confirm(`Run "${action}" on issue #${d.number} as ${botLogin} (reopenable)?`)) return;
    setBusy(true);
    let r: IssueExecResult;
    // A fixer matching the pipeline's recorded one takes the recorded-candidate
    // gate; an operator-typed fixer takes the operator-asserted close gate (live
    // merged-check, no recorded-link requirement). Both land as CLOSE_ISSUE_FIXED.
    if (action === "already-fixed") r = fixer === d.analysis?.fixed_by
      ? await api.closeIssueFixed(d.number, fixer as number, dryRun, commentText || undefined)
      : await api.closeIssue(d.number, { disposition: "fixed", comment: commentText, fixed_by: fixer as number }, dryRun);
    // A canonical matching the pipeline's confirmed close-dup takes the curated
    // gate; any other canonical takes the operator-asserted close gate. Both land
    // as CLOSE_ISSUE_DUP.
    else if (action === "dup") r = d.disposition === "close-dup" && Number(canonical) === d.analysis?.canonical
      ? await api.closeIssueDup(d.number, Number(canonical), dryRun, commentText || undefined)
      : await api.closeIssue(d.number, { disposition: "dup", comment: commentText, canonical: Number(canonical) }, dryRun);
    else if (action === "comment") r = await api.commentIssue(d.number, commentText, dryRun);
    else r = await api.closeIssue(d.number, { disposition: action, comment: commentText }, dryRun);
    setResult(r); setBusy(false); reportResult(r);
    if (landed(r)) onActed?.();
  };

  const reopen = async () => {
    if (!dryRun && !window.confirm(`Reopen issue #${d.number} and remove the bot's closing comment(s)?`)) return;
    setBusy(true);
    const r = await api.reopenIssue(d.number, dryRun);
    setResult(r); setBusy(false); reportResult(r);
    if (landed(r)) onActed?.();
  };

  const fireLabel = action === "comment"
    ? (dryRun ? "Comment (dry)" : "● Comment")
    : (dryRun ? "Close (dry)" : "● Close");

  return (
    <div className="suggestion tone-muted">
      <div className="pr-actions-wrap stacked">
        <div className="pr-actions">
          <select value={action} onChange={(e) => setAction(e.target.value as IAct)} disabled={busy}>
            {OPTS.map((o) => <option key={o.v} value={o.v}>{o.label}</option>)}
          </select>
          {action === "dup" && (
            <input className="canon" placeholder="#" value={canonical}
              onChange={(e) => setCanonical(e.target.value.replace(/\D/g, ""))} disabled={busy} />
          )}
          {action === "already-fixed" && (
            <>
              <span className="muted small">by</span>
              <input className="canon" placeholder="#" value={fixerInput}
                onChange={(e) => setFixerInput(e.target.value.replace(/\D/g, ""))} disabled={busy}
                title="the merged PR that fixed this issue" />
            </>
          )}
          <button className={`btn-primary sm btn-stable ${busy ? "is-busy" : ""} ${!dryRun ? "btn-live" : ""}`}
            onClick={fire} disabled={busy || blocked}
            title={needsCanon ? "enter the canonical issue #" : needsFixer ? "enter the fixer PR #" : needsComment ? "a comment is required" : ""}>
            <span className="btn-stable-label">{fireLabel}</span>
            {busy && <span className="spinner btn-stable-spinner" aria-hidden="true" />}
          </button>
          {d.state === "closed" && (
            <button className="btn-secondary sm" onClick={reopen} disabled={busy}
              title="Undo: reopen + remove the bot's closing comment(s)">↩ Reopen</button>
          )}
          {result && <ExecResultChip result={result} />}
        </div>
        <CommentEditor botLogin={botLogin} value={commentText} isEdited={isEdited}
          open={openComment} onToggle={() => setOpenComment((o) => !o)}
          onChange={setEdited} onReset={() => setEdited(null)}
          placeholder={action === "comment" || action === "not-planned" || action === "completed"
            ? "comment posted to the issue (required)" : "comment posted to the issue (optional)"} />
      </div>
    </div>
  );
}
