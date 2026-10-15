/** The lines a log view holds: the new ones after the old, the newest `cap`. */
export function appendLines(prev: string[], next: string[], cap: number): string[] {
  const all = next.length === 0 ? prev : prev.concat(next);
  return all.length > cap ? all.slice(all.length - cap) : all;
}
