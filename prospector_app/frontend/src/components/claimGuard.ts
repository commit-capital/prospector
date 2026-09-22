import { api } from "../api";
import { timeAgo } from "../timeAgo";

/** Fresh at-action check: resolves true to proceed. Warns when another
 *  operator holds the item's claim right now; an unreachable claims endpoint
 *  never blocks the action. */
export async function confirmUnclaimed(kind: "pr" | "issue", n: number): Promise<boolean> {
  try {
    const { items, me } = await api.claims();
    const c = items[`${kind}:${n}`];
    if (c && c.by !== me.by) {
      return window.confirm(
        `${kind === "pr" ? "PR" : "Issue"} #${n} is claimed by ${c.by} (on ${c.machine}, ${timeAgo(c.at)} ago). Act anyway?`);
    }
  } catch { /* claims unavailable */ }
  return true;
}
