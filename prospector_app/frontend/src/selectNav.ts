export type SelectItem = { value: string; label: string; disabled: boolean; group: string | null; title?: string };

/** The next enabled item from `from` in direction `dir`, or `from` when none
 *  lies that way. A `from` of -1 steps onto the first enabled item. */
export function step(items: readonly SelectItem[], from: number, dir: 1 | -1): number {
  for (let i = from + dir; i >= 0 && i < items.length; i += dir) {
    if (!items[i].disabled) return i;
  }
  return from;
}

export function edge(items: readonly SelectItem[], which: "first" | "last"): number {
  return which === "first" ? step(items, -1, 1) : step(items, items.length, -1);
}

/** The first enabled item after `from` whose label starts with `query`,
 *  wrapping around; -1 when none does. */
export function typeahead(items: readonly SelectItem[], query: string, from: number): number {
  const q = query.toLowerCase();
  for (let k = 1; k <= items.length; k++) {
    const i = (from + k + items.length) % items.length;
    if (!items[i].disabled && items[i].label.toLowerCase().startsWith(q)) return i;
  }
  return -1;
}
