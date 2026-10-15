export const FACT_LABEL: Record<string, string> = {
  signals: "Signals", reviews: "Reviewer feedback", drift: "Drift", summary: "Summary",
  cluster: "Cluster", analysis: "Analysis", security: "Security review",
  greptile_review: "Greptile read", verify: "Verification", threat: "Threat scan",
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
