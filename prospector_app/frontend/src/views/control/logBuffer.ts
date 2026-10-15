/** The lines a log view holds: the new ones after the old, the newest `cap`. */
export function appendLines(prev: string[], next: string[], cap: number): string[] {
  const all = next.length === 0 ? prev : prev.concat(next);
  return all.length > cap ? all.slice(all.length - cap) : all;
}

export const SKIPPED = "… earlier lines skipped: the log rotated or was cut while this was hidden …";

/** The view after one read: a `skipped` read starts over at the log's tail. */
export function nextLines(prev: string[], read: { lines: string[]; skipped: boolean }, cap: number): string[] {
  return read.skipped ? appendLines([SKIPPED], read.lines, cap) : appendLines(prev, read.lines, cap);
}
