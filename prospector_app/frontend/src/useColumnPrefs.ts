import { useEffect, useState } from "react";
import { COLUMNS, type ColumnDef } from "./components/explorer/columns";
import { useExec } from "./ExecContext";

const KEY = "app-explorer-columns";
const DEFAULTS: Record<string, boolean> = Object.fromEntries(COLUMNS.map((c) => [c.key, c.defaultOn]));

// Only explicit user overrides are stored, so a column added in a later release
// always starts at its own defaultOn even for users with saved prefs.
function read(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch { return {}; }
}

export function useColumnPrefs(): {
  isOn: (k: string) => boolean;
  toggle: (k: string) => void;
  reset: () => void;
  visibleColumns: ColumnDef[];
} {
  const { review } = useExec();
  // Columns gated on a backend capability drop out entirely when it's absent (no
  // external review provider → no Greptile column, and it never appears in toggles).
  const available = COLUMNS.filter((c) => c.capability !== "review" || review.provider !== "none");
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
  const visibleColumns = available.filter((c) => c.fixed || isOn(c.key));
  return { isOn, toggle, reset, visibleColumns };
}
