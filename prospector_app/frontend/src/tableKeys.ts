/** j/k row-cursor movement for a keyboard-navigable table: `j` moves down,
 *  `k` up, either key enters an uncursored table at the top, the cursor
 *  clamps at both ends, and an empty table has no cursor. */
export function nextRowIndex(current: number | null, key: "j" | "k", count: number): number | null {
  if (count <= 0) return null;
  if (current == null) return 0;
  const next = key === "j" ? current + 1 : current - 1;
  return Math.min(count - 1, Math.max(0, next));
}

/** Whether a keydown should be left alone by table shortcuts: focused
 *  controls (a link's or button's Enter is a click), typing surfaces, and
 *  open dialogs own their keys, and so does any chorded press. */
export function ignoreTableKey(target: EventTarget | null, e: { metaKey: boolean; ctrlKey: boolean; altKey: boolean }): boolean {
  if (e.metaKey || e.ctrlKey || e.altKey) return true;
  const el = target as { closest?: (selector: string) => unknown } | null;
  if (typeof el?.closest !== "function") return false;
  return el.closest("a, button, input, select, textarea, summary, [contenteditable], [role=\"dialog\"]") != null;
}
