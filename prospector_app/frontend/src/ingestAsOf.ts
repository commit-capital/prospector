/** Staleness reads for charts and counts built from ingested store data: the
 *  store's picture of upstream is only known up to the last successful ingest,
 *  so days past that stamp are unknown, not zero. */

// The local calendar day of `d` as YYYY-MM-DD. Not toISOString(), which is UTC:
// in the evening that's already tomorrow's date, shifting the whole axis a day
// ahead of the backend's local-day buckets.
export function localDay(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

/** Index of the last day in `days` (ascending YYYY-MM-DD) that the ingest
 *  stamp covers. Days after it are unknown. With no parseable stamp every day
 *  reads as covered; -1 when the stamp predates the whole window. */
export function knownUntil(days: string[], asOf: string | null | undefined): number {
  if (!asOf) return days.length - 1;
  const t = Date.parse(asOf);
  if (Number.isNaN(t)) return days.length - 1;
  const day = localDay(new Date(t));
  let idx = -1;
  for (let i = 0; i < days.length; i++) {
    if (days[i] <= day) idx = i;
  }
  return idx;
}

/** A count over a window starting on `windowStart` (YYYY-MM-DD): the number
 *  itself while the ingest stamp reaches into the window, "—" when the whole
 *  window falls after the last ingest — that count is unknown, not zero. */
export function ingestedCount(n: number, asOf: string | null | undefined,
  windowStart: string | undefined): number | string {
  if (!asOf || !windowStart) return n;
  const t = Date.parse(asOf);
  if (Number.isNaN(t)) return n;
  return localDay(new Date(t)) < windowStart ? "—" : n;
}

/** Whether day-axis index `i` of `n` gets a printed label: every
 *  `labelStep`-th day plus the final day, skipping a regular label that would
 *  crowd the final one (closer than half a step). */
export function showAxisLabel(i: number, n: number, labelStep: number): boolean {
  if (i === n - 1) return true;
  return i % labelStep === 0 && n - 1 - i >= labelStep / 2;
}
