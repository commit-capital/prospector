import type { WorkStatus } from "./api";
import { timeAgo } from "./timeAgo.ts";

/** Elapsed run time as "5m"/"3h", or null under a minute — a label that says
 *  "just now" about a run it is watching reads like it stopped. */
function elapsed(iso?: string | null): string | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t) || Date.now() - t < 60_000) return null;
  return timeAgo(iso);
}

/** The header's one-line read of the whole deployment, most urgent signal
 *  only: active runs, then this backend's jobs, then queued work, then the
 *  parked pile, else "idle". The flyout carries the rest. */
export function workStatusLabel(s: WorkStatus): string {
  if (s.active.length === 1) {
    const a = s.active[0];
    const head = `${a.lane} #${a.pr}`;
    if (!a.worker_online) return `${head} · worker offline?`;
    const doing = a.lane === "fix" ? a.action : a.step;
    const age = elapsed(a.started_at);
    return head + (doing ? ` · ${doing}` : "") + (age ? ` · ${age}` : "");
  }
  if (s.active.length > 1) return `${s.active.length} running`;
  if (s.jobs.running > 0) return `${s.jobs.running} job${s.jobs.running > 1 ? "s" : ""}`;
  const queued = s.queued.verify + s.queued.fix;
  if (queued > 0) return `${queued} queued`;
  if (s.awaiting_review > 0) return `${s.awaiting_review} to review`;
  return "idle";
}
