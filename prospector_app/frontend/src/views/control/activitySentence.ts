import type { ActivityLane, MachineActivity } from "../../api";

/** One piece of a machine's sentence. `numbers` are the PRs (or issues) behind
 *  it, for a link; `jobId` names this app's job, whose output it opens. */
export interface PhrasePart {
  text: string;
  numbers?: number[];
  jobId?: number;
  bad?: boolean;
}

/** One clause: `paren` reads "lead (a, b)", `list` reads "lead a, b", `plain`
 *  is the lead alone. */
export interface ActivityPhrase {
  key: string;
  lead: PhrasePart;
  parts: PhrasePart[];
  style: "paren" | "list" | "plain";
  linkKind: "pr" | "issue" | null;
}

const LANES: [ActivityLane, string, string][] = [
  ["security", "security review", "security reviews"],
  ["verify", "verify run", "verify runs"],
  ["autofix", "autofix attempt", "autofix attempts"],
  ["issue_fix", "issue fix", "issue fixes"],
];

const BAD = /^(failed|RED|regressed|error)/;

function dollars(usd: number): string {
  return usd >= 10 ? `$${Math.round(usd)}` : `$${usd.toFixed(2)}`;
}

/** A machine's past day as clauses: each lane with its outcomes, the
 *  background passes, the jobs a person ran, and the AI spend. */
export function activityPhrases(m: MachineActivity): ActivityPhrase[] {
  const out: ActivityPhrase[] = [];
  for (const [lane, one, many] of LANES) {
    const a = m.lanes[lane];
    if (!a || a.count === 0) continue;
    out.push({
      key: lane,
      lead: { text: `${a.count} ${a.count === 1 ? one : many}`, numbers: a.numbers },
      parts: a.outcomes.map((o) => ({ text: `${o.count} ${o.label}`, numbers: o.numbers, bad: BAD.test(o.label) })),
      style: "paren",
      linkKind: lane === "issue_fix" ? "issue" : "pr",
    });
  }
  if (m.background.length > 0) {
    out.push({
      key: "background", lead: { text: "background:" },
      parts: m.background.map((b) => ({ text: `${b.count} ${b.label}` })),
      style: "list", linkKind: null,
    });
  }
  if (m.jobs.length > 0) {
    out.push({
      key: "jobs", lead: { text: m.local ? "you ran" : "ran" },
      parts: m.jobs.map((j) => {
        const suffix = j.status === "failed" ? " (failed)"
          : j.status === "running" || j.status === "queued" ? " (running)" : "";
        const part: PhrasePart = { text: `${j.label}${suffix}`, bad: j.status === "failed" };
        if (j.job_id != null) part.jobId = j.job_id;
        return part;
      }),
      style: "list", linkKind: null,
    });
  }
  if (m.spend_usd >= 0.01) {
    out.push({ key: "spend", lead: { text: `${dollars(m.spend_usd)} AI` }, parts: [], style: "plain", linkKind: null });
  }
  return out;
}

function since(iso: string, now: number): string {
  const min = Math.max(0, Math.floor((now - Date.parse(iso)) / 60_000));
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  return hr < 48 ? `${hr}h` : `${Math.floor(hr / 24)}d`;
}

/** A machine's status chips: online, offline for how long, or no worker, and
 *  any paused lanes. */
export function machineStatus(m: MachineActivity, now: number): { text: string; tone: "ok" | "bad" | "muted" }[] {
  if (m.host === "unattributed") return [{ text: "machine not recorded", tone: "muted" }];
  const out: { text: string; tone: "ok" | "bad" | "muted" }[] = [];
  if (m.online) out.push({ text: "● online", tone: "ok" });
  else if (!m.has_worker) out.push({ text: "no worker", tone: "muted" });
  else out.push({ text: m.offline_since ? `offline ${since(m.offline_since, now)}` : "offline", tone: "bad" });
  if (m.tripped.length > 0) out.push({ text: `${m.tripped.join(", ")} paused`, tone: "bad" });
  return out;
}
