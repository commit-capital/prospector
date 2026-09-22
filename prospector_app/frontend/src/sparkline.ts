/** Pure geometry for the Home progress sparklines: an SVG path over daily
 *  values, and where the stale (post-last-ingest) shading begins. */

/** The x pixel for point `i` of `n` across width `w` inside `pad` margins. */
const xAt = (i: number, n: number, w: number, pad: number): number =>
  n <= 1 ? w / 2 : pad + (i / (n - 1)) * (w - 2 * pad);

/** An SVG path over `values`, scaled to the box; a null breaks the line into
 *  separate segments, and a flat series draws at mid-height. */
export function sparklinePath(values: (number | null)[], w: number, h: number, pad = 2): string {
  const present = values.filter((v): v is number => v !== null);
  if (present.length === 0) return "";
  const min = Math.min(...present);
  const max = Math.max(...present);
  const yAt = (v: number): number =>
    max === min ? h / 2 : pad + (1 - (v - min) / (max - min)) * (h - 2 * pad);
  let path = "";
  let penDown = false;
  values.forEach((v, i) => {
    if (v === null) { penDown = false; return; }
    const pt = `${xAt(i, values.length, w, pad).toFixed(1)},${yAt(v).toFixed(1)}`;
    path += `${path ? " " : ""}${penDown ? "L" : "M"} ${pt}`;
    penDown = true;
  });
  return path;
}

/** The x where staleness shading starts: the first day after the last
 *  ingest's local day. Null when the ingest covers the newest day, or when
 *  there was never an ingest to date against an empty axis. */
export function staleFromX(days: string[], lastIngestAt: string | null,
                           w: number, pad = 2): number | null {
  if (days.length === 0) return null;
  if (lastIngestAt === null) return xAt(0, days.length, w, pad);
  const t = Date.parse(lastIngestAt);
  if (Number.isNaN(t)) return null;
  const d = new Date(t);
  const ingestDay = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const i = days.findIndex((day) => day > ingestDay);
  if (i < 0) return null;
  return xAt(i, days.length, w, pad);
}
