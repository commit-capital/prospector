import type { IssueDisposition, IssueRow } from "../../api";

// The merged PR that fixed an issue: the fix scan's current fixer when the row
// carries one, else an explicit Fixes/Closes/Resolves reference the PR store
// knows merged. A PR the issue's own text names is never taken as the fixer —
// "still broken despite #N" is anti-evidence — and neither is a subsystem match.
// The executor re-verifies the merged state live before it closes anything.
export function issueFixer(row: IssueRow): number | null {
  if (row.fixed_by !== null) return row.fixed_by;
  return row.linked_prs.find((p) => p.how === "explicit" && p.state === "merged")?.pr
    ?? null;
}

// The canonical issue a duplicate closes against; the cluster's canonical
// itself has none.
export function issueCanonical(row: IssueRow): number | null {
  return row.canonical !== null && row.canonical !== row.number ? row.canonical : null;
}

// Each selected issue's own close reference — its fixer PR for `fixed`, its
// canonical for `dup` — plus the selected issues that have none (including any
// not among `rows`), in selection order.
export function perIssueRefs(
  rows: IssueRow[], selected: number[], disposition: IssueDisposition,
): { refs: Record<number, number>; missing: number[] } {
  const byNumber = new Map(rows.map((r) => [r.number, r]));
  const refs: Record<number, number> = {};
  const missing: number[] = [];
  for (const n of selected) {
    const r = byNumber.get(n);
    const ref = r ? (disposition === "fixed" ? issueFixer(r) : issueCanonical(r)) : null;
    if (ref === null) missing.push(n);
    else refs[n] = ref;
  }
  return { refs, missing };
}
