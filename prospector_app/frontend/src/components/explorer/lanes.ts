import type { FilterSpec } from "../../api";
// Explicit .ts extension so the node:test runner (type stripping, no bundler)
// can resolve this runtime import when homeCards.test.ts loads the module.
import { HOME_CARDS } from "../../views/homeCards.ts";

// A lane is a named filter template: clicking its chip drops the template's
// filters into the Explorer spec, where each shows as its own editable,
// clearable chip. Lanes carry no matching logic of their own — the backend
// sees only the plain filter fields.

function cardSpec(key: string): FilterSpec {
  return HOME_CARDS.find((c) => c.key === key)!.spec;
}

export interface Lane {
  key: "easy" | "stale" | "merge-ready" | "needs-human";
  label: string;
  spec: FilterSpec;
}

export const LANES: Lane[] = [
  // Ready to merge, and also tiny and leaf-surface — the fastest possible
  // human approvals.
  { key: "easy", label: "⚡ Easy Lane",
    spec: { ...cardSpec("ready"), risk_tier: 3,
            loc: { metric: "both", scope: "effective", op: "<", value: 20 } } },
  // Feedback stands (review score below the bar, scored against the latest
  // commit) and the author hasn't touched the PR in over a month.
  { key: "stale", label: "🗑️ Stale",
    spec: { age_days: { op: ">", value: 30 },
            greptile: { op: "<", value: 5 }, greptile_stale: false } },
  // Named for the Home cards whose specs they carry, so one population carries
  // one name.
  { key: "merge-ready", label: "✅ Ready to merge", spec: cardSpec("ready") },
  { key: "needs-human", label: "👤 Your call", spec: cardSpec("your-call") },
];
