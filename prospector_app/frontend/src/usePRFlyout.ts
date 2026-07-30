import { useFlyoutParam } from "./useFlyoutParam";

/** The PR detail flyout is driven by a `?pr=N` search param on whatever page
 *  you're on — so opening a PR overlays the current page (cluster, queue, …)
 *  instead of navigating away. Multiple PRs stack as `?pr=N,M` (see #20).
 *  Closing removes the param(s). */
export function usePRFlyout() {
  const { items, open, addPane, closePane, close } = useFlyoutParam("pr", "issue");
  return { prs: items, pr: items[0] ?? null, openPR: open, addPane, closePane, close };
}
