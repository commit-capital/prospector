import type { MouseEvent } from "react";

/** Full-page route for a PR (opened in a new browser tab on modifier-click). */
function prPageUrl(n: number): string {
  return `/prs/${n}`;
}

/** Shared click semantics for a whole clickable PR row.
 *
 *  - plain click → open the PR in the detail flyout (replaces the stack)
 *  - cmd / ctrl click → toggle the row's selection checkbox when `onModSelect`
 *    is supplied (a list with checkboxes); else open an extra stacked pane
 *    (ctrl, if addPane) or the PR's full page in a new tab
 *  - shift click → let the browser do its default (text selection)
 */
export function makeRowOpen(
  openPR: (n: number) => void,
  addPane?: (n: number) => void,
  onModSelect?: (n: number) => void,
) {
  return (n: number) => ({
    className: "rowlink",
    onClick: (e: MouseEvent) => {
      if (e.shiftKey) return;
      if ((e.metaKey || e.ctrlKey) && onModSelect) { e.preventDefault(); onModSelect(n); return; }
      if (e.ctrlKey && addPane) { e.preventDefault(); addPane(n); return; }
      if (e.metaKey || e.ctrlKey) {
        window.open(prPageUrl(n), "_blank", "noopener");
        return;
      }
      openPR(n);
    },
    // middle-click → new tab, matching link conventions
    onAuxClick: (e: MouseEvent) => {
      if (e.button === 1) {
        e.preventDefault();
        window.open(prPageUrl(n), "_blank", "noopener");
      }
    },
  });
}

/** Stop a click on an interactive cell (links, checkboxes, selects, action
 *  buttons) from bubbling up to the row's open handler. */
export function stopRowOpen(e: MouseEvent) {
  e.stopPropagation();
}
