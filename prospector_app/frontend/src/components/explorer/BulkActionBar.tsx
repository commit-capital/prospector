import { useState } from "react";
import { Term } from "../InfoTip";
import { term } from "../../glossary";
import { useExec } from "../../ExecContext";

export type BulkAction = "CLOSE" | "CLOSE_DUP" | "CLOSE_FIXED" | "CLOSE_STALE"
  | "CLOSE_OVERSIZED" | "REQUEST_CHANGES" | "COMMENT" | "GREPTILE_RETRIGGER"
  | "QUEUE_VERIFY" | "RUN_SECURITY" | "MERGE";

const ACTS: { v: BulkAction; label: string }[] = [
  { v: "CLOSE_DUP", label: "close — dup of" },
  { v: "CLOSE_FIXED", label: "close — already-fixed" },
  { v: "CLOSE_STALE", label: "close — stale" },
  { v: "CLOSE_OVERSIZED", label: "close — too big (break up)" },
  { v: "CLOSE", label: "triage close" },
  { v: "REQUEST_CHANGES", label: "request changes" },
  { v: "COMMENT", label: "comment" },
  { v: "QUEUE_VERIFY", label: "queue for verification" },
  { v: "RUN_SECURITY", label: "run security reviews" },
  { v: "MERGE", label: "merge" },
];

export function BulkActionBar({ selected, onApply }:
  { selected: number[]; onApply: (args: {
      action: BulkAction; comment: string; canonical?: number; perPr: boolean }) => void }) {
  const [action, setAction] = useState<BulkAction>("CLOSE_STALE");
  const [comment, setComment] = useState("");
  const [canonical, setCanonical] = useState("");
  // post each PR's own suggested comment instead of one shared comment (#182)
  const [perPr, setPerPr] = useState(false);
  const { botLogin, pushToast, review } = useExec();
  if (selected.length === 0) return null;
  const isMerge = action === "MERGE";
  const postsComment = action !== "MERGE" && action !== "GREPTILE_RETRIGGER"
    && action !== "QUEUE_VERIFY" && action !== "RUN_SECURITY";
  const actions = review.retrigger
    ? [...ACTS.slice(0, -1), { v: "GREPTILE_RETRIGGER" as const, label: `re-trigger ${review.label}` }, ACTS.at(-1)!]
    : ACTS;

  // Local-only utility — no upstream write, so it bypasses onApply/the executor.
  const copyIds = async () => {
    const text = selected.map((n) => `#${n}`).join(", ");
    const count = selected.length;
    try {
      await navigator.clipboard.writeText(text);
      pushToast(`Copied ${count} PR number${count === 1 ? "" : "s"} to clipboard`, "green");
    } catch {
      pushToast("Couldn't copy to clipboard", "red", { detail: text });
    }
  };

  return (
    <div className="bulkbar">
      <span className="cnt">{selected.length} selected</span>
      <Term k="ui.bulk" cue={false}><span className="glo-i" aria-hidden="true">ⓘ</span></Term>
      <button className="btn-secondary sm" onClick={copyIds}
        title="Copy the selected PR numbers (e.g. #1234, #1235) to the clipboard">
        📋 Copy IDs
      </button>
      <select value={action} onChange={(e) => setAction(e.target.value as BulkAction)}
        title="One action applied to every selected PR. Each write is gated per-PR and logged to the activity feed.">
        {actions.map((a) => <option key={a.v} value={a.v} title={term(`bulk.${a.v}`)?.meaning}>{a.label}</option>)}
      </select>
      {action === "CLOSE_DUP" && !perPr && (
        <input className="canon" placeholder="#" value={canonical}
          onChange={(e) => setCanonical(e.target.value.replace(/\D/g, ""))} />
      )}
      {postsComment && (
        <label className="bulk-perpr" title={`Post the comment ${botLogin} suggested for each PR (its own canonical/rationale) instead of one comment for all of them.`}>
          <input type="checkbox" checked={perPr} onChange={(e) => setPerPr(e.target.checked)} />
          each PR's own comment
        </label>
      )}
      {postsComment && !perPr && (
        <input className="cmtbox" placeholder="one shared comment applied to all selected"
          value={comment} onChange={(e) => setComment(e.target.value)} />
      )}
      {postsComment && perPr && <span className="muted small">each PR keeps its own suggested comment</span>}
      {action === "GREPTILE_RETRIGGER" && (
        <span className="muted small">posts the review trigger on each PR — no new commit needed</span>
      )}
      {action === "QUEUE_VERIFY" && (
        <span className="muted small">queues each PR for the sandbox verifier — a local queue, nothing posted upstream</span>
      )}
      {action === "RUN_SECURITY" && (
        <span className="muted small">starts a background security review for each PR — two run at a time</span>
      )}
      {isMerge && <span className="muted small">merge posts no comment — each PR is gated individually</span>}
      <button className="btn-primary sm"
        onClick={() => onApply({ action, comment, canonical: canonical ? Number(canonical) : undefined, perPr })}>
        Apply →
      </button>
    </div>
  );
}
