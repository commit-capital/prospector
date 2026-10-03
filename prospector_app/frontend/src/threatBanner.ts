import type { ThreatSummary } from "./api.ts";

// How many PR numbers a banner phrase names before counting the rest.
const NAMED_LIMIT = 6;

/** What the Threats view narrows to under `?show=`: just what one banner
 *  phrase counted. */
export type ThreatFocus = "malicious" | "credentials";

export interface BannerPart {
  text: string;
  focus: ThreatFocus;
}

export function threatsHref(focus: ThreatFocus): string {
  return `/security/threats?show=${focus}`;
}

function named(prs: number[]): string {
  const shown = prs.slice(0, NAMED_LIMIT).map((n) => `#${n}`).join(", ");
  return prs.length > NAMED_LIMIT ? `${shown} +${prs.length - NAMED_LIMIT}` : shown;
}

/** The threat banner's phrases, or null when there is nothing to raise: the
 *  open PRs flagged malicious and the PRs holding a credential a maintainer
 *  leaked, each named by number. Suspicious PRs and contributors' leaks stay
 *  off the banner. */
export function threatBannerParts(threats: ThreatSummary | undefined): BannerPart[] | null {
  if (!threats) return null;
  const parts: BannerPart[] = [];
  const malicious = threats.malicious.map((f) => f.pr);
  if (malicious.length > 0) {
    const n = malicious.length;
    parts.push({ text: `${n} open PR${n === 1 ? "" : "s"} flagged malicious: ${named(malicious)}`,
                 focus: "malicious" });
  }
  if (threats.secrets.length > 0) {
    const n = threats.secrets.length;
    const where = n === 1 ? "a maintainer's PR" : "maintainers' PRs";
    parts.push({ text: `${n} credential${n === 1 ? "" : "s"} leaked in ${where}: ${named(threats.secrets)}`,
                 focus: "credentials" });
  }
  return parts.length ? parts : null;
}
