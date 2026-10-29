import type { TripAlert } from "./api.ts";

// How much of a trip's reason the banner shows; the rest is on hover.
const REASON_LIMIT = 160;

/** The banner's headline for one alert: which lane, on which machine, and
 *  whether it is stopped now or keeps reopening onto the same fault. */
export function tripHeadline(a: TripAlert): string {
  const kind = a.kind ?? "unknown";
  if (!a.tripped) return `${a.lane} lane on ${a.host} tripped ${a.trips_24h}× in 24h · ${kind}`;
  const again = a.trips_24h > 1 ? ` · ${a.trips_24h}× in 24h` : "";
  return `${a.lane} lane stopped on ${a.host} · ${kind}${again}`;
}

export function tripReason(a: TripAlert): string {
  const r = a.reason ?? "";
  return r.length > REASON_LIMIT ? `${r.slice(0, REASON_LIMIT)}…` : r;
}
