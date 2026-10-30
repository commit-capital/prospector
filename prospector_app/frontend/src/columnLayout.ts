/** Column order and widths a user set by dragging table headers. */
import type { DragEvent, MouseEvent } from "react";

/** `columns` in the user's saved `order`. A column the order doesn't name
 *  (one added after it was saved) keeps its place after its default
 *  predecessor. */
export function orderColumns<C extends { key: string }>(columns: readonly C[], order: readonly string[]): C[] {
  const byKey = new Map(columns.map((c) => [c.key, c]));
  const out = order.flatMap((k) => byKey.get(k) ?? []);
  columns.forEach((c, i) => {
    if (out.includes(c)) return;
    out.splice(i === 0 ? 0 : out.indexOf(columns[i - 1]) + 1, 0, c);
  });
  return out;
}

/** `keys` with `from` moved into `to`'s place: after it when dragged
 *  rightward, before it when dragged leftward. */
export function moveKey(keys: readonly string[], from: string, to: string): string[] {
  const i = keys.indexOf(from);
  const j = keys.indexOf(to);
  if (i < 0 || j < 0 || i === j) return [...keys];
  const out = keys.filter((k) => k !== from);
  out.splice(j, 0, from);
  return out;
}

export const MIN_COL_WIDTH = 40;

/** CSS pinning each sized column of `.tableClass` to its width. Cells carry a
 *  `c-<key>` class, so a resize restyles them without re-rendering rows. */
export function widthCss(tableClass: string, widths: Readonly<Record<string, number>>): string {
  return Object.entries(widths)
    .filter(([k]) => /^[\w-]+$/.test(k))
    .map(([k, w]) => {
      const px = `${Math.max(MIN_COL_WIDTH, Math.round(w))}px`;
      return `.${tableClass} :is(th, td).c-${k} { width: ${px}; min-width: ${px}; max-width: ${px}; overflow: hidden; text-overflow: ellipsis; }`;
    })
    .join("\n");
}

// A header drag carries its column key under this type, so drops from
// elsewhere (files, text) are ignored.
const DRAG_TYPE = "application/x-prospector-column";
let resizing = false;

/** Props making a `<th>` a drag source and drop target for reordering. */
export function columnDragProps(key: string, move: (from: string, to: string) => void): {
  draggable: true;
  onDragStart: (e: DragEvent<HTMLElement>) => void;
  onDragOver: (e: DragEvent<HTMLElement>) => void;
  onDragLeave: (e: DragEvent<HTMLElement>) => void;
  onDrop: (e: DragEvent<HTMLElement>) => void;
} {
  const accepts = (e: DragEvent<HTMLElement>) => e.dataTransfer.types.includes(DRAG_TYPE);
  return {
    draggable: true,
    onDragStart: (e) => {
      if (resizing) { e.preventDefault(); return; }
      e.dataTransfer.setData(DRAG_TYPE, key);
      e.dataTransfer.effectAllowed = "move";
    },
    onDragOver: (e) => {
      if (!accepts(e)) return;
      e.preventDefault();
      e.currentTarget.classList.add("col-drop-target");
    },
    onDragLeave: (e) => e.currentTarget.classList.remove("col-drop-target"),
    onDrop: (e) => {
      e.currentTarget.classList.remove("col-drop-target");
      const from = e.dataTransfer.getData(DRAG_TYPE);
      if (!from) return;
      e.preventDefault();
      move(from, key);
    },
  };
}

/** Drags the right edge of the header cell holding the handle `e` pressed,
 *  reporting each new width. The click that ends the drag doesn't reach the
 *  header, so it never sorts. */
export function startColumnResize(e: MouseEvent<HTMLElement>, onWidth: (w: number) => void): void {
  e.preventDefault();
  e.stopPropagation();
  const th = e.currentTarget.closest("th");
  if (!th) return;
  const startX = e.clientX;
  const startW = th.getBoundingClientRect().width;
  resizing = true;
  document.body.classList.add("col-resizing");
  const onMove = (ev: globalThis.MouseEvent) => onWidth(Math.max(MIN_COL_WIDTH, startW + ev.clientX - startX));
  const swallow = (ev: globalThis.MouseEvent) => ev.stopPropagation();
  const onUp = () => {
    resizing = false;
    document.body.classList.remove("col-resizing");
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
    window.addEventListener("click", swallow, true);
    setTimeout(() => window.removeEventListener("click", swallow, true), 0);
  };
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
}
