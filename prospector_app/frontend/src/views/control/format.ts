/** Time and tone helpers shared by the Control tab's panels. */

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.floor(ms / 60_000);
  if (min < 2) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const d = Math.floor(hr / 24);
  return `${d}d ago`;
}

export function fmt(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16).replace("+00", " UTC");
}

/** A rough "~Ns"/"~Nm"/"~Nh" wall-clock estimate, or null when there isn't
 *  enough run history yet (or the projected count is zero) to show one. */
export function fmtDuration(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return null;
  if (seconds < 60) return `~${Math.max(1, Math.round(seconds))}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `~${minutes < 10 ? minutes.toFixed(1) : Math.round(minutes)}m`;
  const hours = minutes / 60;
  return `~${hours < 10 ? hours.toFixed(1) : Math.round(hours)}h`;
}

/** An exact elapsed time — "42s", "4m 05s", "1h 03m" — for a running job. */
export function fmtElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

/** How long a running action has been going, ticking between polls. A
 *  mechanical rebase takes a minute or two, which "just now" cannot show. */
export function elapsed(startedAt: string | null | undefined, now: number): string {
  if (!startedAt) return "—";
  const s = Math.max(0, Math.round((now - new Date(startedAt).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

export function agoColor(iso: string | null | undefined): string {
  if (!iso) return "chip chip-muted";
  const hr = (Date.now() - new Date(iso).getTime()) / 3_600_000;
  if (hr < 48) return "chip chip-green";
  if (hr < 168) return "chip chip-amber";
  return "chip chip-red";
}

/** Chip tone for a queued/running/waiting-for-base verify request. */
export function queueStatusChip(status: string): string {
  if (status === "running" || status === "pushing") return "chip chip-blue queue-running";
  if (status === "waiting-for-base") return "chip chip-amber";
  return "chip chip-muted";
}

/** Chip tone for an autofix row: a live status reads as progress, an ending
 *  reads as its outcome. */
export function fixStatusChip(status: string): string {
  if (status === "pushed") return "chip chip-green";
  if (status === "failed") return "chip chip-red";
  if (status === "refused") return "chip chip-amber";
  if (status === "cancelled") return "chip chip-muted";
  return queueStatusChip(status);
}

/** Chip tone for a run's result: security verdicts map straight to their
 *  color; a fix run's result is its terminal status, so it takes the same tone
 *  the queue row does; verify outcomes are green when the fix is confirmed, red
 *  on error/regression, amber for every in-between state. */
export function resultChip(phase: string, result: string | null | undefined): string {
  if (!result) return "chip chip-muted";
  if (phase === "security") {
    if (result === "GREEN") return "chip chip-green";
    if (result === "YELLOW") return "chip chip-amber";
    if (result === "RED") return "chip chip-red";
    return "chip chip-muted";
  }
  if (phase === "fix") return fixStatusChip(result);
  if (result === "verified-fix" || result === "agent-verified") return "chip chip-green";
  if (result.startsWith("error") || result === "regressed") return "chip chip-red";
  return "chip chip-amber";
}

/** A time in the viewer's zone: "3:00 PM" today, "Thu 3:00 PM" on another day. */
export function localTime(iso: string): string {
  const d = new Date(iso);
  const today = d.toDateString() === new Date().toDateString();
  return d.toLocaleString([], today
    ? { hour: "numeric", minute: "2-digit" }
    : { weekday: "short", hour: "numeric", minute: "2-digit" });
}

export function ageText(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86_400)}d`;
}

export const percent = (f: number): string => `${Math.round(f * 100)}%`;
/** A 0–1 fraction as a CSS length along a bar, held to the bar's ends. */
export const along = (f: number): string => `${Math.min(Math.max(f, 0), 1) * 100}%`;

export const RANGE_OPTIONS = [
  { label: "7 days", days: 7, allTime: false },
  { label: "30 days", days: 30, allTime: false },
  { label: "90 days", days: 90, allTime: false },
  { label: "all time", days: 7, allTime: true },
] as const;
export type RangeOpt = (typeof RANGE_OPTIONS)[number];
