import type { IssuePR } from "../../api";

// The link-evidence kinds where something names the PR and the issue together,
// each mapped to the tooltip line explaining that evidence. Mirrors
// issue_links.REFERENCED in Python — the set the row's referenced_pr_count is
// summed over. A PR that only shares the issue's subsystem tag, or names it in a
// body without claiming to fix it, is weak evidence and takes no line here.
export const EVIDENCE: Record<string, string> = {
  explicit: "explicit Fixes/Closes/Resolves reference in the PR body",
  github: "GitHub lists this PR as closing the issue",
  "fix-found": "merged fix attributed to this issue by the already-fixed detector",
  "issue-ref": "referenced from the issue's own text",
};

// One source, so a rendered kind always has a tooltip line.
export const REFERENCED = new Set(Object.keys(EVIDENCE));

// The state chip beside a linked PR: purple for merged, muted for closed, muted
// "draft" for an open PR GitHub still marks draft. An ordinary open PR, and one
// whose state nothing knows, take no chip.
export function linkStateChip(p: IssuePR): { label: string; cls: string } | null {
  if (p.state === "merged") return { label: "merged", cls: "chip-purple" };
  if (p.state === "closed") return { label: "closed", cls: "chip-muted" };
  if (p.state === "open" && p.draft) return { label: "draft", cls: "chip-muted" };
  return null;
}
