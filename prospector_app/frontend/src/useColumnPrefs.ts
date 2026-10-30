import { useEffect, useMemo, useState } from "react";
import type { ColumnMeta } from "./components/shared/columns";
import { useExec } from "./ExecContext";
import { moveKey, orderColumns } from "./columnLayout";

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

function readJson<T>(storageKey: string, fallback: T): T {
  try { return (JSON.parse(localStorage.getItem(storageKey) ?? "null") as T | null) ?? fallback; } catch { return fallback; }
}

// Persist whenever `value` changes — survives reloads; private-mode safe.
function usePersist(storageKey: string, value: unknown): void {
  useEffect(() => {
    try { localStorage.setItem(storageKey, JSON.stringify(value)); } catch { /* private mode: keep in-memory */ }
  }, [storageKey, value]);
}

/** Which of `columns` a table shows, in what order and at what widths, saved
 *  in this browser under `storageKey`. `columns` must keep one identity across
 *  renders (a module constant). */
export function useColumnPrefs<C extends ColumnMeta>(columns: readonly C[], storageKey: string): {
  isOn: (k: string) => boolean;
  toggle: (k: string) => void;
  hide: (k: string) => void;
  move: (from: string, to: string) => void;
  widths: Record<string, number>;
  setWidth: (k: string, w: number | null) => void;
  reset: () => void;
  orderedColumns: C[];
  visibleColumns: C[];
} {
  const { activeReviewers } = useExec();
  const defaults = useMemo(
    () => Object.fromEntries(columns.map((c) => [c.key, c.defaultOn])) as Record<string, boolean>,
    [columns]);
  const [overrides, setOverrides] = useState<Record<string, boolean>>(() => read(storageKey));
  const [order, setOrder] = useState<string[]>(() => readJson(`${storageKey}-order`, []));
  const [widths, setWidths] = useState<Record<string, number>>(() => readJson(`${storageKey}-widths`, {}));
  usePersist(storageKey, overrides);
  usePersist(`${storageKey}-order`, order);
  usePersist(`${storageKey}-widths`, widths);
  const isOn = (k: string) => overrides[k] ?? defaults[k] ?? false;
  // Functional updater so several toggles in one tick build on the latest state
  // instead of a stale closure clobbering each other.
  const toggle = (k: string) =>
    setOverrides((prev) => ({ ...prev, [k]: !(prev[k] ?? defaults[k] ?? false) }));
  const hide = (k: string) => setOverrides((prev) => ({ ...prev, [k]: false }));
  const orderedColumns = useMemo(() => orderColumns(columns, order), [columns, order]);
  const move = (from: string, to: string) =>
    setOrder((prev) => moveKey(orderColumns(columns, prev).map((c) => c.key), from, to));
  const setWidth = (k: string, w: number | null) => setWidths((prev) => {
    const next = { ...prev };
    if (w === null) delete next[k]; else next[k] = Math.round(w);
    return next;
  });
  const reset = () => { setOverrides({}); setOrder([]); setWidths({}); };
  // Columns gated on a reviewer kind drop out entirely while no reviewer of
  // that kind is active (and never appear in toggles). Memoized for a stable
  // identity across renders — the PR Explorer keys its memoized table rows on
  // this array, so it must only change when the selection or order does.
  const visibleColumns = useMemo(
    () => orderedColumns.filter((c) => !c.capability || activeReviewers(c.capability).length > 0)
      .filter((c) => c.fixed || (overrides[c.key] ?? defaults[c.key] ?? false)),
    [orderedColumns, defaults, overrides, activeReviewers]);
  return { isOn, toggle, hide, move, widths, setWidth, reset, orderedColumns, visibleColumns };
}
