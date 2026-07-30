import { useFlyoutParam } from "./useFlyoutParam";

/** The issue detail flyout is driven by a `?issue=N` search param on whatever
 *  page you're on — opening an issue overlays the current page (Issues table,
 *  dup card, …). One pane only; closing removes the param. */
export function useIssueFlyout() {
  const { items, open, close } = useFlyoutParam("issue", "pr");
  return { issue: items[0] ?? null, openIssue: open, close };
}
