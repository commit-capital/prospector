import type { ThreatSummary } from "./api.ts";

// How many flagged PR numbers the banner names before counting the rest.
const NAMED_LIMIT = 6;

/** The threat banner's phrases, or null when there is nothing to raise: the
 *  open PRs flagged malicious, named by number, and the leaked credentials
 *  still to rotate. Suspicious PRs alone stay off the banner. */
export function threatBannerParts(threats: ThreatSummary | undefined): string[] | null {
  if (!threats) return null;
  const parts: string[] = [];
  const n = threats.malicious.length;
  if (n > 0) {
    const named = threats.malicious.slice(0, NAMED_LIMIT).map((f) => `#${f.pr}`).join(", ");
    const more = n > NAMED_LIMIT ? ` +${n - NAMED_LIMIT}` : "";
    parts.push(`${n} open PR${n === 1 ? "" : "s"} flagged malicious: ${named}${more}`);
  }
  if (threats.secrets > 0) {
    parts.push(`${threats.secrets} leaked credential${threats.secrets === 1 ? "" : "s"} to rotate`);
  }
  return parts.length ? parts : null;
}
