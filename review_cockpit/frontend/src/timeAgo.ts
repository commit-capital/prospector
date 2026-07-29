/** Compact relative time: "just now", "5m", "3h", "2d", "4w", else a date.
 *  For the PR-queue "Updated" column and anywhere a terse recency read helps. */
export function timeAgo(iso?: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  const m = s / 60;
  if (m < 60) return `${Math.floor(m)}m`;
  const h = m / 60;
  if (h < 24) return `${Math.floor(h)}h`;
  const d = h / 24;
  if (d < 7) return `${Math.floor(d)}d`;
  const w = d / 7;
  if (w < 5) return `${Math.floor(w)}w`;
  return new Date(t).toISOString().slice(0, 10); // YYYY-MM-DD
}

/** A logged UTC timestamp rendered in the viewer's own local time as
 *  "YYYY-MM-DD HH:MM:SS" — so a teammate's action reads on your clock and the
 *  feed's instant-ordering matches the times shown. Falls back to the raw value
 *  for a legacy stamp that won't parse. */
export function localDateTime(iso?: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso.replace("T", " ");
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
