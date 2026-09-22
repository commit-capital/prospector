/** Grouping for the "needs attention" list on the Control tab: the auto-hunt's
 *  failed runs folded by lane and failure reason, so 56 PR chips read as a few
 *  reasons with counts. */

export interface AttentionGroup {
  lane: "security" | "verify";
  reason: string;
  prs: number[];
}

/** Failed hunt runs grouped by (lane, reason), largest group first. A security
 *  failure's reason is its recorded skip reason; a verify failure's is its
 *  error kind; either falls back to "run failed" when unrecorded. */
export function groupNeedsAttention(
  securityFailed: number[],
  securityReasons: Record<string, string>,
  verifyFailed: { pr: number; error_kind?: string | null }[],
): AttentionGroup[] {
  const groups = new Map<string, AttentionGroup>();
  const add = (lane: AttentionGroup["lane"], reason: string, pr: number) => {
    const key = `${lane}:${reason}`;
    const g = groups.get(key);
    if (g) g.prs.push(pr);
    else groups.set(key, { lane, reason, prs: [pr] });
  };
  for (const pr of securityFailed) {
    add("security", securityReasons[String(pr)]?.trim() || "run failed", pr);
  }
  for (const f of verifyFailed) {
    add("verify", f.error_kind?.trim() || "run failed", f.pr);
  }
  return [...groups.values()].sort((a, b) => b.prs.length - a.prs.length);
}
