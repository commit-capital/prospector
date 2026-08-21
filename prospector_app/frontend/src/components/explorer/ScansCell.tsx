import type { ReviewDigest } from "../../api";
import { InfoTip } from "../InfoTip";
import { useExec } from "../../ExecContext";
import { chipText, STATUS_CHIP } from "./reviewChip";

/** One chip per active security scanner on the row; hover lists each scanner's
 *  check runs, open findings and (Superagent) contributor-trust score. */
export function ScansCell({ reviews }: { reviews: Record<string, ReviewDigest> | null }) {
  const { reviewers } = useExec();
  const active = reviewers.filter((r) => r.kind === "scanner" && r.active);
  const digests = active.map((r) => reviews?.[r.id]).filter((d): d is ReviewDigest => !!d);
  if (digests.length === 0) return <>—</>;
  const body = (
    <div className="tip-list">
      {digests.map((d) => {
        const trust = typeof d.extra.trust_score === "number" ? d.extra.trust_score : null;
        const verdict = typeof d.extra.trust_verdict === "string" ? d.extra.trust_verdict : null;
        return (
          <div key={d.id} className={`tip-check tip-${d.status === "pass" ? "pass" : d.status === "fail" ? "fail" : "warn"}`}>
            <span className="tip-mark">{d.status === "pass" ? "✓" : d.status === "fail" ? "✗" : d.status === "na" ? "•" : "!"}</span>
            <span className="tip-name">
              {d.summary_line}{d.reason && d.status !== "pass" ? ` — ${d.reason}` : ""}
              {trust != null && <div className="muted small">contributor trust {trust}/100{verdict ? ` · ${verdict}` : ""}</div>}
              {d.checks.map((c, i) => (
                <div key={i} className="muted small">{c.conclusion === "success" ? "✓" : c.conclusion === "neutral" ? "•" : c.status !== "completed" ? "…" : "✗"} {c.name}{c.title ? ` — ${c.title}` : ""}</div>
              ))}
            </span>
          </div>
        );
      })}
    </div>
  );
  return (
    <InfoTip body={body} cue={false} focusable={false}>
      <span style={{ display: "inline-flex", gap: 4 }}>
        {digests.map((d) => (
          <span key={d.id} className={`chip ${STATUS_CHIP[d.status] ?? "chip-muted"} sm`}
            title={`${d.label}: ${d.summary_line}`}>
            {digests.length > 1 ? `${d.label} ` : ""}{d.status === "pass" ? "✓" : d.status === "na" ? "—" : chipText(d)}
          </span>
        ))}
      </span>
    </InfoTip>
  );
}
