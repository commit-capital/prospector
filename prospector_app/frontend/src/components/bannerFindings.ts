import type { SafetyFinding } from "../api";

const SEV_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };

/** The security findings the verdict banner names inline, most severe first,
 *  and how many more the Checks panel's security row holds. */
export function bannerFindings(findings: SafetyFinding[], limit = 3): { shown: SafetyFinding[]; more: number } {
  const ranked = [...findings].sort((a, b) =>
    (SEV_RANK[a.severity?.toLowerCase() ?? ""] ?? 4) - (SEV_RANK[b.severity?.toLowerCase() ?? ""] ?? 4));
  return { shown: ranked.slice(0, limit), more: Math.max(0, ranked.length - limit) };
}
