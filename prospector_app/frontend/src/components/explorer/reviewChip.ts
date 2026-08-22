import type { ReviewDigest } from "../../api";

// The chip text for one reviewer digest: a score when the reviewer scores,
// else its open findings by severity, else its bar status.
export function chipText(d: ReviewDigest): string {
  if (d.score != null && d.score_max != null) return `${d.score}/${d.score_max}`;
  const open = Object.entries(d.open);
  if (open.length > 0) return open.map(([sev, n]) => `${n} ${sev}`).join(", ");
  return d.status;
}

export const STATUS_CHIP: Record<string, string> = {
  pass: "chip-green", fail: "chip-red", stale: "chip-yellow", pending: "chip-muted", na: "chip-muted",
};
