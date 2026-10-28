import type { PRRow } from "../api";

/** Where the PR stands with the automation, as a sentence (automation.classify). */
export function statusText(r: PRRow): string | null {
  const reason = r.automation?.reason;
  return reason ? reason[0].toUpperCase() + reason.slice(1) : null;
}
