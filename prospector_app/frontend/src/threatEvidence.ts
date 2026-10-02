import type { ThreatEvidence } from "./api";

/** What one evidence capture's line on the PR page says. */
export interface EvidenceLine {
  state: "complete" | "partial";
  head: string;
  /** The force-push that produced the captured head, and the head it replaced. */
  replaced: { at: string | null; before: string } | null;
}

export function evidenceLine(e: ThreatEvidence): EvidenceLine {
  const push = [...e.force_pushes].reverse().find((p) => p.after === e.head_sha && p.before);
  return {
    state: e.complete ? "complete" : "partial",
    head: e.head_sha.slice(0, 7),
    replaced: push?.before ? { at: push.at, before: push.before.slice(0, 7) } : null,
  };
}
