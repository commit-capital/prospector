import type { AutomationState, PRRow } from "../api";

/** Where the PR stands with the automation, as a sentence (automation.classify). */
export function statusText(r: PRRow): string | null {
  const reason = r.automation?.reason;
  return reason ? reason[0].toUpperCase() + reason.slice(1) : null;
}

const STATE_LABEL: Record<AutomationState, string> = {
  moving: "Moving", waiting: "Waiting", ready: "Ready", "needs-you": "Needs you", stalled: "Stalled",
};
const STATE_TONE: Record<AutomationState, "blue" | "muted" | "green" | "yellow" | "red"> = {
  moving: "blue", waiting: "muted", ready: "green", "needs-you": "yellow", stalled: "red",
};

/** The state a PR's status leads with, as its pill reads. */
export function stateLabel(s: AutomationState): string {
  return STATE_LABEL[s];
}

/** The chip tone of a state's pill. */
export function stateTone(s: AutomationState): "blue" | "muted" | "green" | "yellow" | "red" {
  return STATE_TONE[s];
}
