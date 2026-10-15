import type { ItemRef, OpenAuthorReturn } from "../api";
import { CLOSE_REASON_LABEL } from "../closeReasons";
import { useIssueFlyout } from "../useIssueFlyout";
import { useRepoMeta } from "../RepoMetaContext";
import { PRLink } from "./PRLink";

/** A PR or issue reference that opens its detail flyout (⌘-click for GitHub). */
export function ItemRefLink({ item }: { item: Pick<ItemRef, "kind" | "number"> }) {
  const { openIssue } = useIssueFlyout();
  const { issueUrl } = useRepoMeta();
  if (item.kind === "pr") return <PRLink n={item.number} className="mono">PR #{item.number}</PRLink>;
  return (
    <a href={issueUrl(item.number)} target="_blank" rel="noreferrer" className="mono"
      title="Open in this panel (⌘-click for GitHub ↗)"
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        e.stopPropagation();
        openIssue(item.number);
      }}>issue #{item.number}</a>
  );
}

/** One line on an open row saying how its author came back: "↩ reopened
 *  after our stale close" or "↪ follow-up to PR #297 (stale close)". */
export function AuthorReturnNote({ ret }: { ret: OpenAuthorReturn }) {
  const why = ret.reason ? `${CLOSE_REASON_LABEL[ret.reason] ?? ret.reason} close` : "close";
  if (ret.signal === "reopened") return <span>↩ reopened after our {why}</span>;
  return <span>↪ follow-up to <ItemRefLink item={ret.closed} /> ({why})</span>;
}
