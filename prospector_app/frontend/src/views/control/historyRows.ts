import type { AutohuntRun } from "../../api";

const when = (r: AutohuntRun): number => Date.parse(r.finished ?? r.started ?? "") || 0;

/** The security and verify runs and the autofix runs as one history, newest first. */
export function mergeRuns(hunt: AutohuntRun[], fix: AutohuntRun[]): AutohuntRun[] {
  return [...hunt, ...fix].sort((a, b) => when(b) - when(a));
}

/** How many runs of each lane ended with each result; an unrecorded result reads "—". */
export function countResults(rows: AutohuntRun[]): Record<AutohuntRun["phase"], Record<string, number>> {
  const out: Record<AutohuntRun["phase"], Record<string, number>> = { security: {}, verify: {}, fix: {} };
  for (const r of rows) {
    const key = r.result ?? "—";
    out[r.phase][key] = (out[r.phase][key] ?? 0) + 1;
  }
  return out;
}
