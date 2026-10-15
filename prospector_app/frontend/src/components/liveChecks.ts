import type { ChecksRollup, Divergence } from "../api";

/** Whether the live check found conflicts the stored snapshot does not hold. */
export function liveConflicts(diverged: Divergence[] | undefined): boolean {
  return diverged?.some((d) => d.kind === "conflicts") ?? false;
}

/** The checks rollup with the live check's conflicts applied to its
 *  "No merge conflicts" row, so the panel agrees with the banner above it
 *  before the persisted state is read back. */
export function withLiveConflicts(c: ChecksRollup | undefined,
                                  diverged: Divergence[] | undefined): ChecksRollup | undefined {
  if (!c || !liveConflicts(diverged)) return c;
  const checks = c.checks.map((chk) => (chk.key === "mergeable"
    ? { ...chk, status: "fail" as const, detail: "has conflicts (just checked on GitHub)", at: new Date().toISOString() }
    : chk));
  const ran = checks.filter((chk) => chk.status !== "na" && !chk.advisory);
  return { checks, passed: ran.filter((chk) => chk.status === "pass").length, total: ran.length };
}
