import type { AutomationState, NextStep } from "../api";

/** What a PR's status block offers in each state. */
export type StatusAction =
  | "run-now" | "merge" | "quick-click" | "ask-agent" | "re-attempt" | "accept" | "abandon";

// The hunter lanes an operator can run by hand right away.
const MANUAL_LANES: ReadonlySet<NextStep["lane"]> = new Set(["security", "design", "verify"]);

/** The actions a PR in `state` is offered; `lane` is the hunter queue a moving
 *  PR waits in, or null when none holds it. */
export function actionsFor(state: AutomationState, lane: NextStep["lane"] | null): StatusAction[] {
  switch (state) {
    case "moving": return lane && MANUAL_LANES.has(lane) ? ["run-now"] : [];
    case "waiting": return [];
    case "ready": return ["merge"];
    case "needs-you": return ["quick-click"];
    case "stalled": return ["ask-agent", "re-attempt", "accept", "abandon"];
  }
}
