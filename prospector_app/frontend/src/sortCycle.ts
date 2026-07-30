export type SortDir = "asc" | "desc";

/** Click-to-sort state machine shared by every sortable table (PR Explorer,
 *  Issues, #494): a click on a new column sorts its primary direction (desc
 *  for quality/size/recency columns, asc for text columns — the caller's
 *  `descFirst` set), a second click on the same column flips direction, a
 *  third clears the sort (the query engine then falls back to its default
 *  order). */
export function cycleSort<K extends string>(
  current: { key: K | ""; dir: SortDir | "" },
  target: K,
  descFirst: Set<K>,
): { key: K | ""; dir: SortDir | "" } {
  const primary: SortDir = descFirst.has(target) ? "desc" : "asc";
  if (current.key !== target) return { key: target, dir: primary };
  if (current.dir === primary) return { key: target, dir: primary === "desc" ? "asc" : "desc" };
  return { key: "", dir: "" };
}
