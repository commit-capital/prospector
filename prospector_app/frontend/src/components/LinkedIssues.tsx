import type { ReactNode } from "react";
import type { IssueLink } from "../api";
import { useIssueFlyout } from "../useIssueFlyout";
import { useRepoMeta } from "../RepoMetaContext";

const EVIDENCE: Record<string, string> = {
  explicit: "explicit Fixes/Closes/Resolves reference in the PR body",
  "fix-found": "merged fix attributed to this issue by the already-fixed detector",
  "issue-ref": "the issue references this PR",
};

export function LinkedIssues({ issues, limit }: { issues: IssueLink[] | undefined; limit?: number }): ReactNode {
  const { openIssue } = useIssueFlyout();
  const { issueUrl } = useRepoMeta();
  const fixing = (issues ?? []).filter((issue) => issue.how in EVIDENCE);
  const shown = limit == null ? fixing : fixing.slice(0, limit);

  if (!fixing.length) return <span className="muted">—</span>;
  return (
    <span className="issue-prs">
      {shown.map((issue) => (
        <a key={issue.issue} href={issueUrl(issue.issue)} target="_blank" rel="noreferrer"
          className="gh-pr-link" title={`${EVIDENCE[issue.how]} · open in this panel (⌘-click for GitHub ↗)`}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey) return;
            e.preventDefault();
            openIssue(issue.issue);
          }}>
          #{issue.issue}
        </a>
      ))}
      {shown.length < fixing.length && <span className="muted small">+{fixing.length - shown.length}</span>}
    </span>
  );
}
