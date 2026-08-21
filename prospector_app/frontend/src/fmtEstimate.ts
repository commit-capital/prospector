/** A rough "~Ns"/"~Nm"/"~Nh" wall-clock estimate, or null when there isn't a
 *  usable duration to show. */
export function fmtEstimate(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return null;
  if (seconds < 60) return `~${Math.max(1, Math.round(seconds))}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `~${minutes < 10 ? minutes.toFixed(1) : Math.round(minutes)}m`;
  const hours = minutes / 60;
  return `~${hours < 10 ? hours.toFixed(1) : Math.round(hours)}h`;
}
