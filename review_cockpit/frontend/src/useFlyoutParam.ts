import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";

/** Core of a search-param-driven detail flyout: `?<key>=N` (or a comma list
 *  `N,M` for stacked panes) overlays the current page instead of navigating
 *  away. Each flyout hook wraps this with its own param name and surface.
 *  Only one flyout kind shows at a time, so opening one clears the rival
 *  flyout's param (`alsoClear`). */
export function useFlyoutParam(key: string, alsoClear?: string) {
  const [sp, setSp] = useSearchParams();
  const raw = sp.get(key);
  const items = useMemo(
    () => raw ? raw.split(",").map(Number).filter((n) => Number.isFinite(n) && n > 0) : [],
    [raw]);

  const commit = useCallback((list: number[]) => {
    const next = new URLSearchParams(sp);
    const uniq = Array.from(new Set(list));
    if (uniq.length) {
      next.set(key, uniq.join(","));
      if (alsoClear) next.delete(alsoClear);
    } else {
      next.delete(key);
    }
    setSp(next);
  }, [sp, setSp, key, alsoClear]);

  // plain open → replace the stack with this one item
  const open = useCallback((n: number) => commit([n]), [commit]);
  // add a stacked pane (no-op if already open)
  const addPane = useCallback((n: number) => commit(items.includes(n) ? items : [...items, n]), [commit, items]);
  const closePane = useCallback((n: number) => commit(items.filter((p) => p !== n)), [commit, items]);
  const close = useCallback(() => commit([]), [commit]);

  return { items, open, addPane, closePane, close };
}
