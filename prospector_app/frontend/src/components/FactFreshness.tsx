import type { FactFreshness, StaleBlock } from "../api";
import { Collapsible } from "./Collapsible";
import { InfoTip } from "./InfoTip";
import { term } from "../glossary";
import { ago, FACT_LABEL as LABEL, factLine } from "../factLine";

/** Provenance for every fact the PR carries, folded to one line — "1 of 8
 *  facts stale · Security review 8d ago" — that expands to the full table:
 *  when each fact was computed, the head it describes, and whether it still
 *  holds. Answers "when is this recommendation from, and does it still
 *  apply?" — which a rationale alone cannot settle. */
export function FactFreshnessPanel({ facts, headSha, liveHeadSha }: {
  facts?: FactFreshness[];
  headSha?: string | null;
  liveHeadSha?: string | null;
}) {
  if (!facts?.length) return null;
  const stale = facts.filter((f) => !f.current);
  return (
    <section className="prc-section fact-line">
      <Collapsible tone={liveHeadSha ? "red" : stale.length ? "yellow" : undefined}
        summary={factLine(facts, headSha, liveHeadSha)}>
        <div className="muted small" style={{ marginBottom: 6 }}>
          <InfoTip entry={term("freshness.provenance")}>Where these facts come from</InfoTip>
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
      </Collapsible>
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
