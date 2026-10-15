/** Wording for the Health & queues overview: lane states, the Next cell, and
 *  the one-line status of every machine. */
import type { ControlOverview, OverviewCellState, OverviewLane } from "../../api";

const CELL: Record<OverviewCellState, string> = {
  on: "on", off: "off", tripped: "tripped", "ai-paused": "AI paused",
  "stood-down": "stood down", offline: "offline",
};

export function cellLabel(state: OverviewCellState): string {
  return CELL[state];
}

export function nextText(lane: OverviewLane, aiRetryAt: string | null): string {
  const head = lane.queue[0];
  if (head) return `#${head.pr ?? head.issue} · ${lane.queue.length} queued`;
  const running = lane.cells.some((c) => c.state === "on" || c.state === "tripped" || c.state === "ai-paused");
  if (lane.cells.length > 0 && !running) return "—";
  if (lane.cells.length > 0 && lane.cells.every((c) => c.state === "ai-paused")) {
    return aiRetryAt ? "after AI resumes" : "waiting for AI";
  }
  if (lane.pool != null && lane.pool > 0) return `${lane.pool} in hunter pool`;
  return lane.pool != null || lane.cells.some((c) => c.state === "on") ? "nothing waiting" : "—";
}

export function statusLine(o: ControlOverview): string {
  const parts = o.machines.map((m) =>
    m.state === "working" ? `${m.host} working on ${m.lanes_on} lane${m.lanes_on === 1 ? "" : "s"}`
      : m.state === "idle" ? `${m.host} idle`
        : m.state === "off" ? `${m.host} off`
          : `${m.host} offline`);
  if (o.ai_paused) parts.push(`background AI paused: ${o.ai_paused.reason}`);
  return parts.join(" · ");
}
