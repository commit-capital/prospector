import { useState } from "react";
import { api } from "../../api";
import { InfoTip } from "../InfoTip";
import { useExec } from "../../ExecContext";
import type { GlossaryEntry } from "../../glossary";

type Feedback = { score?: number | null; body?: string; commit_id?: string | null };

// The body of a Greptile review can be long; cap it so the hover card stays sane.
const MAX = 600;

// The Greptile cell shows the score inline and lazily fetches THIS PR's actual
// Greptile review summary on first hover. The review body isn't on the list row
// (it's a per-PR fetch), so the popup explains *why* the score is what it
// is rather than repeating the generic definition.
const SEVERITY_BADGE: Record<string, { icon: string; label: string }> = {
  defects: { icon: "🐛", label: "Greptile flagged a real defect, not just style" },
  nits: { icon: "🧹", label: "Greptile's feedback was nitpicks only" },
};

export function GreptileCell({
  n,
  score,
  stale,
  severity,
}: {
  n: number;
  score: number | string | null;
  stale?: boolean | null;
  severity?: "defects" | "nits" | "clean" | null;
}) {
  const { review } = useExec();
  const max = review.score_max ?? 5;
  const bar = review.threshold ?? max;
  const [fb, setFb] = useState<Feedback | "loading" | null>(null);
  const load = () => {
    if (fb !== null) return;            // fetch once per cell; the backend caches per PR too
    setFb("loading");
    api.prGreptile(n).then((d) => setFb(d ?? {})).catch(() => setFb({}));
  };
  if (score == null) return <>—</>;

  const loaded = fb && fb !== "loading" ? fb : null;
  const raw = loaded?.body?.trim();
  const body = raw && raw.length > MAX ? `${raw.slice(0, MAX).trimEnd()}…` : raw;

  const staleNote = stale === true
    ? "\n\n⚠️ Score is from an older commit — the author may have pushed fixes since Greptile reviewed."
    : "";
  const entry: GlossaryEntry = {
    title: "Greptile review",
    meaning: fb === "loading"
      ? "Loading this PR's Greptile review…"
      : loaded
        ? (body || "Greptile scored this PR but left no written summary.") + staleNote
        : `Greptile's AI code-review confidence (0–${max}). Hover loads this PR's review summary.`,
    note: `We require ${bar}/${max} to merge; below that is request-changes.`,
  };
  const badge = severity ? SEVERITY_BADGE[severity] : undefined;
  return (
    <span onMouseEnter={load} onFocus={load} style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
      <InfoTip entry={entry} cue={false} focusable={false}><span>{score}/{max}</span></InfoTip>
      {stale === true && (
        <span title="Greptile score is from an older commit" style={{ color: "var(--yellow, #f59e0b)", fontSize: "0.8em", lineHeight: 1 }}>
          ⚠
        </span>
      )}
      {badge && (
        <span title={badge.label} style={{ fontSize: "0.8em", lineHeight: 1 }}>
          {badge.icon}
        </span>
      )}
    </span>
  );
}
