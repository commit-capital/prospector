import type { AutohuntRun } from "../../api";

const when = (r: AutohuntRun): number => Date.parse(r.finished ?? r.started ?? "") || 0;

/** The security and verify runs and the autofix runs as one history, newest first. */
export function mergeRuns(hunt: AutohuntRun[], fix: AutohuntRun[]): AutohuntRun[] {
  return [...hunt, ...fix].sort((a, b) => when(b) - when(a));
}
