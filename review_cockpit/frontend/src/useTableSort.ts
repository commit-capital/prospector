import { useState } from "react";

// Shared client-side table sort (#180). A table passes a map of column key →
// value accessor; the hook owns the {column, direction} state, the header
// click/indicator helpers, and a `sorted()` that returns a re-ordered copy.
// Server-paged tables (PR Explorer) sort in the backend instead — this is for
// fully-loaded client tables (Cluster board, Action items, cluster buckets).

type SortDir = "asc" | "desc";
type SortValue = string | number | boolean | null | undefined;
export type Accessor<T> = (row: T) => SortValue;

export interface TableSort<T> {
  column: string;
  dir: SortDir;
  clickSort: (col: string) => void;
  indicator: (col: string) => string;
  /** Props to spread onto a sortable <th> (class, click, aria-sort). */
  thProps: (col: string) => {
    className: string;
    onClick: () => void;
    role: "button";
    "aria-sort": "ascending" | "descending" | "none";
    title: string;
  };
  sorted: (rows: T[]) => T[];
}

interface Opts<T> {
  initial?: string;
  initialDir?: SortDir;
  descFirst?: string[];          // columns whose first click sorts descending
  tiebreak?: (a: T, b: T) => number;
}

// nullish always sorts last (in BOTH directions); numbers/booleans numerically;
// strings case-insensitively.
function compare(a: SortValue, b: SortValue): number {
  const an = a == null, bn = b == null;
  if (an || bn) return an && bn ? 0 : an ? 1 : -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" || typeof b === "boolean")
    return (a ? 1 : 0) - (b ? 1 : 0);
  return String(a).toLowerCase().localeCompare(String(b).toLowerCase());
}

export function useTableSort<T>(
  accessors: Record<string, Accessor<T>>,
  opts: Opts<T> = {},
): TableSort<T> {
  const [column, setColumn] = useState(opts.initial ?? "");
  const [dir, setDir] = useState<SortDir>(opts.initialDir ?? "asc");

  const clickSort = (col: string) => {
    if (column === col) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setColumn(col); setDir(opts.descFirst?.includes(col) ? "desc" : "asc"); }
  };
  const indicator = (col: string) => (column === col ? (dir === "asc" ? " ▲" : " ▼") : "");
  const thProps = (col: string) => ({
    className: `sortable-th ${column === col ? "sorted" : ""}`,
    onClick: () => clickSort(col),
    role: "button" as const,
    "aria-sort": (column === col ? (dir === "asc" ? "ascending" : "descending") : "none") as
      "ascending" | "descending" | "none",
    title: "Click to sort by this column",
  });

  const sorted = (rows: T[]): T[] => {
    const acc = accessors[column];
    if (!acc) return rows;
    const mul = dir === "asc" ? 1 : -1;
    // nullish stays last regardless of direction, so only the real-value
    // comparison flips with `mul`; the tiebreak keeps the sort stable.
    return [...rows].sort((a, b) => {
      const va = acc(a), vb = acc(b);
      const an = va == null, bn = vb == null;
      if (an || bn) {
        if (an && bn) return opts.tiebreak?.(a, b) ?? 0;
        return an ? 1 : -1;
      }
      const c = compare(va, vb);
      return c !== 0 ? c * mul : (opts.tiebreak?.(a, b) ?? 0);
    });
  };

  return { column, dir, clickSort, indicator, thProps, sorted };
}
