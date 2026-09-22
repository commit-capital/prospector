/** Chip text and tone for one close-dup coverage-map entry. */

export interface DupConcernRef {
  coverage: "landed" | "pr" | "unique";
  covered_by?: number;
  landed_sha?: string;
}

export function coverageTone(coverage: DupConcernRef["coverage"]): "green" | "blue" | "red" {
  return coverage === "landed" ? "green" : coverage === "pr" ? "blue" : "red";
}

export function coverageLabel(c: DupConcernRef): string {
  if (c.coverage === "landed") {
    return c.landed_sha ? `landed ${c.landed_sha.slice(0, 7)}` : "landed";
  }
  if (c.coverage === "pr") {
    return c.covered_by != null ? `PR #${c.covered_by}` : "another PR";
  }
  return "unique";
}
