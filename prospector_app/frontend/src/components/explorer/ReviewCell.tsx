import { useState } from "react";
import { api, type ReviewDigest, type ReviewEntry } from "../../api";
import { InfoTip } from "../InfoTip";
import { useExec } from "../../ExecContext";
import { chipText, STATUS_CHIP } from "./reviewChip";

// A reviewer's summary can be long; cap the hover excerpt so the card stays sane.
const MAX = 600;

const SEVERITY_BADGE: Record<string, { icon: string; label: string }> = {
  defects: { icon: "🐛", label: "Greptile flagged a real defect, not just style" },
  nits: { icon: "🧹", label: "Greptile's feedback was nitpicks only" },
};

/** One chip per active code reviewer on the row; hover shows each reviewer's
 *  verdict line and, lazily, the stored summary excerpt. */
export function ReviewCell({ n, reviews, greptileSeverity }: {
  n: number;
  reviews: Record<string, ReviewDigest> | null;
  greptileSeverity?: "defects" | "nits" | "clean" | null;
}) {
  const { reviewers } = useExec();
  const [detail, setDetail] = useState<Record<string, { entry: ReviewEntry | null; digest: ReviewDigest }> | "loading" | null>(null);
  const active = reviewers.filter((r) => r.kind === "review" && r.active);
  const digests = active.map((r) => reviews?.[r.id]).filter((d): d is ReviewDigest => !!d);
  if (digests.length === 0) return <>—</>;
  const load = () => {
    if (detail !== null) return;
    setDetail("loading");
    api.prReviews(n).then((d) => setDetail(d.reviews ?? {})).catch(() => setDetail({}));
  };
  const loaded = detail && detail !== "loading" ? detail : null;
  const body = (
    <div className="tip-list">
      {digests.map((d) => {
        const raw = loaded?.[d.id]?.entry?.summary?.trim();
        const excerpt = raw && raw.length > MAX ? `${raw.slice(0, MAX).trimEnd()}…` : raw;
        return (
          <div key={d.id} className={`tip-check tip-${d.status === "pass" ? "pass" : d.status === "fail" ? "fail" : "warn"}`}>
            <span className="tip-mark">{d.status === "pass" ? "✓" : d.status === "fail" ? "✗" : "!"}</span>
            <span className="tip-name">
              {d.summary_line}{d.reason && d.status !== "pass" ? ` — ${d.reason}` : ""}
              {detail === "loading" && <span className="muted small"> (loading…)</span>}
              {excerpt && <div className="muted small" style={{ whiteSpace: "pre-wrap", marginTop: 4 }}>{excerpt}</div>}
            </span>
          </div>
        );
      })}
    </div>
  );
  return (
    <span onMouseEnter={load} onFocus={load} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <InfoTip body={body} cue={false} focusable={false}>
        <span style={{ display: "inline-flex", gap: 4 }}>
          {digests.map((d) => {
            const badge = d.id === "greptile" && greptileSeverity ? SEVERITY_BADGE[greptileSeverity] : undefined;
            return (
              <span key={d.id} className={`chip ${STATUS_CHIP[d.status] ?? "chip-muted"} sm`}
                title={`${d.label}: ${d.summary_line}`}>
                {digests.length > 1 ? `${d.label} ` : ""}{chipText(d)}
                {d.stale === true && <span style={{ color: "var(--yellow, #f59e0b)" }}> ⚠</span>}
                {badge && <span title={badge.label}> {badge.icon}</span>}
              </span>
            );
          })}
        </span>
      </InfoTip>
    </span>
  );
}
