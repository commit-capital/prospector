import type { PRDetail } from "./api";

// Every action the operator can take on a PR, in one menu. Reopen is the ↩
// undo button beside the fire button, shown once an undoable action landed.
export type Act = "MERGE" | "APPROVE" | "REQUEST_CHANGES" | "COMMENT"
  | "CLOSE" | "CLOSE_DUP" | "CLOSE_FIXED" | "CLOSE_STALE" | "CLOSE_OVERSIZED";

/** The agent's recommended disposition → the matching dropdown action, so the
 *  bar opens pre-selected on what the agent suggests. A blocked merge pick
 *  still selects merge: the primary button then reads "Merge blocked" with the
 *  gate's reason under it, matching the verdict instead of defaulting to a
 *  comment. */
export function suggestedAct(pr: PRDetail): Act {
  const a = pr.suggestion?.accept;
  if (!a) return pr.suggestion?.action === "BLOCKED" ? "MERGE" : "COMMENT";
  if (a.kind === "merge") return "MERGE";
  if (a.kind === "close") return (a.action as Act) || "CLOSE";
  // review
  return a.event === "approve" ? "APPROVE" : a.event === "request-changes" ? "REQUEST_CHANGES" : "COMMENT";
}

/** The canonical PR the agent identified for a close-dup, so the bar opens with
 *  the "#" already filled — and the comment preview cites it — instead of an
 *  empty box that previews the neutral "duplicate during triage" wording (#195). */
export function suggestedCanonical(pr: PRDetail): string {
  const a = pr.suggestion?.accept;
  return a?.kind === "close" && a.canonical ? String(a.canonical) : "";
}
