import type { FilterSpec } from "../../api";
// Explicit .ts extension so the node:test runner (type stripping, no bundler)
// can resolve this runtime import when homeCards.test.ts loads the module.
import { ALL_CHECKS_PASS } from "./checkDefs.ts";

// A lane is a named filter template: clicking its chip drops the template's
// filters into the Explorer spec, where each shows as its own editable,
// clearable chip. Lanes carry no matching logic of their own — the backend
// sees only the plain filter fields.

// Every gate green and ready to merge right now — the `review` and `scans`
// check rows carry every active reviewer's and scanner's bar. The Home tab's
// "PRs ready to merge" card narrows this same spec to the pipeline's merge picks.
export const MERGE_READY_SPEC: FilterSpec = {
  checks: ALL_CHECKS_PASS,
  safety: "GREEN",
};

export interface Lane {
  key: "easy" | "stale" | "merge-ready" | "needs-human";
  label: string;
  spec: FilterSpec;
}

export const LANES: Lane[] = [
  // Merge-ready, and also tiny, leaf-surface, and a pipeline merge pick — the
  // fastest possible human approvals.
  { key: "easy", label: "⚡ Easy Lane",
    spec: { ...MERGE_READY_SPEC, risk_tier: 3, disposition: "merge",
            loc: { metric: "both", scope: "effective", op: "<", value: 20 } } },
  // Feedback stands (review score below the bar, scored against the latest
  // commit) and the author hasn't touched the PR in over a month.
  { key: "stale", label: "🗑️ Stale",
    spec: { age_days: { op: ">", value: 30 },
            greptile: { op: "<", value: 5 }, greptile_stale: false } },
  { key: "merge-ready", label: "✅ Merge-ready", spec: MERGE_READY_SPEC },
  // A RED security verdict already reads as needs-human (gates.forced_disposition),
  // so the single disposition filter covers those too.
  { key: "needs-human", label: "👤 Needs human",
    spec: { disposition: "needs-human" } },
];
