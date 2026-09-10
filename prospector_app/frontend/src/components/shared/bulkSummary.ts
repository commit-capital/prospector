export function chipTone(status: string): string {
  if (status === "done" || status === "executed" || status === "merged") return "green";
  if (status === "failed" || status === "error" || status === "blocked") return "red";
  if (status === "running") return "blue";
  if (status === "tracking-lost") return "amber";
  return "muted";
}

// "3 executed · 1 skipped" from a status → count map.
export function summaryLine(counts: Record<string, number>): string {
  return Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(" · ");
}

export function countStatuses(statuses: string[]): Record<string, number> {
  return statuses.reduce<Record<string, number>>((counts, s) => {
    counts[s] = (counts[s] ?? 0) + 1;
    return counts;
  }, {});
}
