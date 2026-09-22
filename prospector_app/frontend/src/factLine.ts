import type { FactFreshness } from "./api";

export const FACT_LABEL: Record<string, string> = {
  signals: "Signals", reviews: "Reviewer feedback", drift: "Drift", summary: "Summary",
  cluster: "Cluster", analysis: "Analysis", security: "Security review",
  greptile_review: "Greptile read", verify: "Verification",
};

/** Human-readable age of an ISO stamp: "2h ago", "3d ago". */
export function ago(at?: string | null): string {
  if (!at) return "undated";
  const then = new Date(at).getTime();
  if (Number.isNaN(then)) return "undated";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 60) return `${Math.max(mins, 0)}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** The provenance panel's one-line summary. A moved head outranks staleness
 *  (every fact then describes the earlier code); otherwise the line names the
 *  most out-of-date stale fact, so it reads as the age of the weakest
 *  evidence rather than a bare count. */
export function factLine(facts: FactFreshness[], headSha?: string | null,
                         liveHeadSha?: string | null): string {
  const stale = facts.filter((f) => !f.current);
  if (liveHeadSha) {
    return `Author pushed since — these facts describe ${headSha?.slice(0, 7) ?? "an earlier head"}`;
  }
  const oldest = stale.slice()
    .sort((a, b) => (a.checked_at ?? "").localeCompare(b.checked_at ?? ""))[0];
  if (!oldest) return `All ${facts.length} facts current`;
  return `${stale.length} of ${facts.length} facts stale · ${FACT_LABEL[oldest.section] ?? oldest.section} ${ago(oldest.checked_at)}`;
}
