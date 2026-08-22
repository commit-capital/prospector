import { useEffect, useMemo, useState } from "react";
import { COLUMNS, type ColumnDef } from "./components/explorer/columns";
import { useExec } from "./ExecContext";

const KEY = "app-explorer-columns";
const DEFAULTS: Record<string, boolean> = Object.fromEntries(COLUMNS.map((c) => [c.key, c.defaultOn]));

// Only explicit user overrides are stored, so a column added in a later release
// always starts at its own defaultOn even for users with saved prefs. A stored
// `greptile` key is read as the `review` column it became.
function read(): Record<string, boolean> {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}") as Record<string, boolean>;
    if ("greptile" in raw && !("review" in raw)) raw.review = raw.greptile;
    delete raw.greptile;
    return raw;
  } catch { return {}; }
}

export function useColumnPrefs(): {
  isOn: (k: string) => boolean;
  toggle: (k: string) => void;
  reset: () => void;
  visibleColumns: ColumnDef[];
} {
  const { activeReviewers } = useExec();
  const [overrides, setOverrides] = useState<Record<string, boolean>>(read);
  // Persist whenever the override map changes — survives reloads; private-mode safe.
  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify(overrides)); } catch { /* private mode: keep in-memory */ }
  }, [overrides]);
  const isOn = (k: string) => overrides[k] ?? DEFAULTS[k] ?? false;
  // Functional updater so several toggles in one tick build on the latest state
  // instead of a stale closure clobbering each other.
  const toggle = (k: string) =>
    setOverrides((prev) => ({ ...prev, [k]: !(prev[k] ?? DEFAULTS[k] ?? false) }));
  const reset = () => setOverrides({});
  // Columns gated on a reviewer kind drop out entirely while no reviewer of
  // that kind is active (and never appear in toggles). Memoized for a stable
  // identity across renders — the PR Explorer keys its memoized table rows on
  // this array, so it must only change when the selection does.
  const visibleColumns = useMemo(
    () => COLUMNS.filter((c) => !c.capability || activeReviewers(c.capability).length > 0)
      .filter((c) => c.fixed || (overrides[c.key] ?? DEFAULTS[c.key] ?? false)),
    [overrides, activeReviewers]);
  return { isOn, toggle, reset, visibleColumns };
}
