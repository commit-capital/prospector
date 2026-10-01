import { useIssueFlyout } from "../useIssueFlyout";
import { useRepoMeta } from "../RepoMetaContext";

// An issue reference: a plain click opens that issue in the flyout over the
// current page; a modifier-click follows the href to github.com.
export function FlyoutIssueLink({ n }: { n: number }) {
  const { openIssue } = useIssueFlyout();
  const { issueUrl } = useRepoMeta();
  return (
    <a href={issueUrl(n)} target="_blank" rel="noreferrer"
       className="gh-pr-link" title="Open in this panel (⌘-click for GitHub ↗)"
       onClick={(e) => {
         if (e.metaKey || e.ctrlKey || e.shiftKey) return;
         e.preventDefault(); openIssue(n);
       }}>#{n}</a>
  );
}
