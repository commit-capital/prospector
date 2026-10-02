import type { ThreatEvidence } from "./api";

/** What one evidence capture's line on the PR page says. */
export interface EvidenceLine {
  state: "complete" | "partial";
  head: string;
  /** The head the captured one replaced, and when a GitHub force-push replaced
   *  it (null when Prospector's diff cache named that head instead). */
  replaced: { at: string | null; before: string } | null;
}

export function evidenceLine(e: ThreatEvidence): EvidenceLine {
  const push = [...e.force_pushes].reverse().find((p) => p.after === e.head_sha && p.before);
  const prior = e.artifacts.prior?.before_sha;
  const replaced = push?.before
    ? { at: push.at, before: push.before.slice(0, 7) }
    : prior ? { at: null, before: prior.slice(0, 7) } : null;
  return { state: e.complete ? "complete" : "partial", head: e.head_sha.slice(0, 7), replaced };
}

/** Whether the page should offer to capture again: no capture is complete. */
export function needsRetry(items: ThreatEvidence[]): boolean {
  return !items.some((e) => e.complete);
}
