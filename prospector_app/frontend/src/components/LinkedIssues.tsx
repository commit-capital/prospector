import type { ReactNode } from "react";
import type { IssueLink } from "../api";
import { useIssueFlyout } from "../useIssueFlyout";
import { useRepoMeta } from "../RepoMetaContext";
import { FIX_EVIDENCE, shownIssues } from "./linkedIssuesShown";

export function LinkedIssues({ issues, limit }: { issues: IssueLink[] | undefined; limit?: number }): ReactNode {
  const { openIssue } = useIssueFlyout();
  const { issueUrl } = useRepoMeta();
  const { shown, hidden } = shownIssues(issues, limit);

  if (!shown.length && !hidden) return <span className="muted">—</span>;
  return (
    <span className="issue-prs">
      {shown.map((issue) => (
        <a key={issue.issue} href={issueUrl(issue.issue)} target="_blank" rel="noreferrer"
          className="gh-pr-link" title={`${FIX_EVIDENCE[issue.how]} · open in this panel (⌘-click for GitHub ↗)`}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey) return;
            e.preventDefault();
            openIssue(issue.issue);
          }}>
          #{issue.issue}
        </a>
      ))}
      {hidden > 0 && <span className="muted small">+{hidden}</span>}
    </span>
  );
}
