import type { StaleBlock } from "../api";
import { ago, FACT_LABEL as LABEL } from "../factLine";

/** The confirm shown when the executor refuses a write because the PR moved on.
 *  Names what changed and which facts it invalidated, so the choice to post
 *  anyway is an informed one. */
export function StaleOverrideConfirm({ block, detail, onConfirm, onCancel, busy }: {
  block: StaleBlock;
  detail?: string;
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
}) {
  return (
    <div className="fresh-callout" role="alertdialog" aria-label="stale evidence confirmation">
      <div><strong>⚠ Not posted — the PR moved since this was analyzed.</strong></div>
      <div>
        Analyzed at <code>{block.was?.slice(0, 7) ?? "?"}</code>, now at{" "}
        <code>{block.now?.slice(0, 7) ?? "?"}</code>.
      </div>
      {block.sections.length > 0 && (
        <div className="muted small" style={{ marginTop: 4 }}>
          Facts that describe the earlier code:{" "}
          {block.sections.map((s) => `${LABEL[s.section] ?? s.section} (${ago(s.checked_at)})`).join(", ")}
        </div>
      )}
      {detail && <div className="muted small">{detail}</div>}
      <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
        <button className="btn-secondary sm" onClick={onCancel} disabled={busy}>
          Cancel — re-analyze first
        </button>
        <button className="btn-stop sm" onClick={onConfirm} disabled={busy}>
          {busy ? "Posting…" : "Post anyway"}
        </button>
      </div>
    </div>
  );
}
