import { useEffect, useMemo, useState } from "react";
import type { ColumnMeta } from "./components/shared/columns";
import { useExec } from "./ExecContext";

// Only explicit user overrides are stored, so a column added in a later release
// always starts at its own defaultOn even for users with saved prefs. A stored
// `greptile` key is read as the `review` column it became.
function read(storageKey: string): Record<string, boolean> {
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey) || "{}") as Record<string, boolean>;
    if ("greptile" in raw && !("review" in raw)) raw.review = raw.greptile;
    delete raw.greptile;
    return raw;
  } catch { return {}; }
}

/** Which of `columns` a table shows, saved in this browser under `storageKey`.
 *  `columns` must keep one identity across renders (a module constant). */
export function useColumnPrefs<C extends ColumnMeta>(columns: readonly C[], storageKey: string): {
  isOn: (k: string) => boolean;
  toggle: (k: string) => void;
  reset: () => void;
  visibleColumns: C[];
} {
  const { activeReviewers } = useExec();
  const defaults = useMemo(
    () => Object.fromEntries(columns.map((c) => [c.key, c.defaultOn])) as Record<string, boolean>,
    [columns]);
  const [overrides, setOverrides] = useState<Record<string, boolean>>(() => read(storageKey));
  // Persist whenever the override map changes — survives reloads; private-mode safe.
  useEffect(() => {
    try { localStorage.setItem(storageKey, JSON.stringify(overrides)); } catch { /* private mode: keep in-memory */ }
  }, [overrides, storageKey]);
  const isOn = (k: string) => overrides[k] ?? defaults[k] ?? false;
  // Functional updater so several toggles in one tick build on the latest state
  // instead of a stale closure clobbering each other.
  const toggle = (k: string) =>
    setOverrides((prev) => ({ ...prev, [k]: !(prev[k] ?? defaults[k] ?? false) }));
  const reset = () => setOverrides({});
  // Columns gated on a reviewer kind drop out entirely while no reviewer of
  // that kind is active (and never appear in toggles). Memoized for a stable
  // identity across renders — the PR Explorer keys its memoized table rows on
  // this array, so it must only change when the selection does.
  const visibleColumns = useMemo(
    () => columns.filter((c) => !c.capability || activeReviewers(c.capability).length > 0)
      .filter((c) => c.fixed || (overrides[c.key] ?? defaults[c.key] ?? false)),
    [columns, defaults, overrides, activeReviewers]);
  return { isOn, toggle, reset, visibleColumns };
}
