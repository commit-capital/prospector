/** Compact relative time: "just now", "5m", "3h", "2d", "4w", "3mo", "2y".
 *  For the PR-queue "Updated" column and anywhere a terse recency read helps —
 *  always relative, so an age column never mixes formats. */
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
  const mo = d / 30.44;
  if (mo < 12) return `${Math.floor(mo)}mo`;
  return `${Math.floor(d / 365.25)}y`;
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

/** A timestamp as a short local date and time, "Sep 10, 9:42 PM", with the
 *  year added when it is not the current one. Empty for a missing or
 *  unparsable stamp. */
export function shortDateTime(iso?: string | null, now: Date = new Date()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const d = new Date(t);
  const date = d.toLocaleDateString([], {
    month: "short", day: "numeric",
    ...(d.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
  return `${date}, ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}
