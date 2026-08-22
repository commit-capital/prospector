import type { FactFreshness, StaleBlock } from "../api";
import { InfoTip } from "./InfoTip";
import { term } from "../glossary";

/** Human-readable age of an ISO stamp: "2h ago", "3d ago". */
function ago(at?: string | null): string {
  if (!at) return "undated";
  const then = new Date(at).getTime();
  if (Number.isNaN(then)) return "undated";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 60) return `${Math.max(mins, 0)}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

const LABEL: Record<string, string> = {
  signals: "Signals", reviews: "Reviewer feedback", drift: "Drift", summary: "Summary",
  cluster: "Cluster", analysis: "Analysis", security: "Security review",
  greptile_review: "Greptile read", verify: "Verification",
};

/** Provenance for every fact the PR carries: when each was computed, the head it
 *  describes, and whether it still holds. Answers "when is this recommendation
 *  from, and does it still apply?" — which a rationale alone cannot settle. */
export function FactFreshnessPanel({ facts, headSha, liveHeadSha }: {
  facts?: FactFreshness[];
  headSha?: string | null;
  liveHeadSha?: string | null;
}) {
  if (!facts?.length) return null;
  const stale = facts.filter((f) => !f.current);
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>
          <InfoTip entry={term("freshness.provenance")}>Where these facts come from</InfoTip>
        </h3>
        <div className="panel-actions">
          {stale.length
            ? <span className="chip chip-amber sm">{stale.length} of {facts.length} stale</span>
            : <span className="chip chip-green sm">all current</span>}
        </div>
      </div>
      {liveHeadSha && (
        <div className="fresh-callout" role="alert">
          ⚠ The author has pushed since these facts were computed — analyzed at{" "}
          <code>{headSha?.slice(0, 7)}</code>, now at <code>{liveHeadSha.slice(0, 7)}</code>.
          Everything below describes the earlier code.
        </div>
      )}
      <table className="facts-table">
        <thead>
          <tr><th>Fact</th><th>Computed</th><th>Against</th><th>Status</th></tr>
        </thead>
        <tbody>
          {facts.map((f) => (
            <tr key={f.section} className={f.current ? undefined : "fact-stale"}>
              <td>{LABEL[f.section] ?? f.section}</td>
              <td title={f.checked_at ?? undefined}>{ago(f.checked_at)}</td>
              <td><code>{f.against_head_sha?.slice(0, 7) ?? "—"}</code></td>
              <td>{f.current ? <span className="chip chip-green sm">current</span>
                             : <span className="chip chip-amber sm">{f.why ?? "stale"}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

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
