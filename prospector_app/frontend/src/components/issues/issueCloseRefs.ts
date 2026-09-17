import type { IssueDisposition, IssueRow } from "../../api";

// The kinds where the PR itself claims to close the issue. A PR the issue's own
// text names is never the fixer ("still broken despite #N" is anti-evidence),
// and a detector-found fixer counts only through the fix scan's own verdict
// (row.fixed_by). The executor re-verifies the merged state live.
const CLAIMS_FIX = new Set(["explicit", "github"]);

export function issueFixer(row: IssueRow): number | null {
  if (row.fixed_by !== null) return row.fixed_by;
  return row.linked_prs.find((p) => CLAIMS_FIX.has(p.how ?? "") && p.state === "merged")?.pr
    ?? null;
}

export function issueCanonical(row: IssueRow): number | null {
  return row.canonical !== null && row.canonical !== row.number ? row.canonical : null;
}

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
