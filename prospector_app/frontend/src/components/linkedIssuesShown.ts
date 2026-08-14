import type { IssueLink } from "../api";

// Link-evidence kinds that mean "this PR fixes the issue", each mapped to the
// tooltip line explaining that evidence; links with any other kind are not fix
// links and are never rendered.
export const FIX_EVIDENCE: Record<string, string> = {
  explicit: "explicit Fixes/Closes/Resolves reference in the PR body",
  "fix-found": "merged fix attributed to this issue by the already-fixed detector",
  "issue-ref": "the issue references this PR",
};

// The fix-linked issues a list renders: the first `limit` fix links (all of
// them when no limit is given), plus how many more sit behind the "+N"
// overflow marker.
export function shownIssues(
  issues: IssueLink[] | undefined,
  limit?: number,
): { shown: IssueLink[]; hidden: number } {
  const fixing = (issues ?? []).filter((issue) => issue.how in FIX_EVIDENCE);
  const shown = limit == null ? fixing : fixing.slice(0, limit);
  return { shown, hidden: fixing.length - shown.length };
}
