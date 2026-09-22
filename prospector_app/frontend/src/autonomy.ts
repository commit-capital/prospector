/** The header's autonomy disclosure, derived from this machine's worker lane
 *  switches (the same .env keys the Setup tab writes). `unattendedActions`
 *  gives the short nouns the header pill shows; `unattendedDetail` the full
 *  sentences its tooltip carries; `unattendedPushes` whether any switch pushes
 *  to contributor branches without asking, which is when the push identity
 *  belongs in the disclosure. */

const autopushParts = (flags: Record<string, string>): Set<string> =>
  new Set((flags["TRIAGE_FIX_AUTOPUSH"] ?? "").split(",").map((p) => p.trim()).filter(Boolean));

const on = (flags: Record<string, string>, key: string): boolean => (flags[key] ?? "") !== "";

export function unattendedActions(flags: Record<string, string>): string[] {
  const push = autopushParts(flags);
  const out: string[] = [];
  if (push.has("update") || push.has("rebase") || on(flags, "TRIAGE_FIX_AUTOHUNT")) out.push("rebases");
  if (push.has("resolve") || on(flags, "TRIAGE_FIX_HUNT_RESOLVE")) out.push("conflicts");
  if (push.has("fix") || on(flags, "TRIAGE_FIX_HUNT_FIX")) out.push("fixes");
  if (push.has("describe")) out.push("descriptions");
  if (on(flags, "TRIAGE_VERIFY_AUTOHUNT")) out.push("tests");
  return out;
}

export function unattendedDetail(flags: Record<string, string>): string[] {
  const push = autopushParts(flags);
  const out: string[] = [];
  if (push.has("update") || push.has("rebase")) out.push("pushes branch updates without asking");
  else if (on(flags, "TRIAGE_FIX_AUTOHUNT")) out.push("queues branch updates on its own");
  if (push.has("resolve")) out.push("pushes agent-resolved conflicts without asking");
  else if (on(flags, "TRIAGE_FIX_HUNT_RESOLVE")) out.push("resolves conflicts on its own (each waits for approval)");
  if (push.has("fix")) out.push("pushes cleared agent fixes without asking");
  else if (on(flags, "TRIAGE_FIX_HUNT_FIX")) out.push("drafts fixes on its own (each waits for approval)");
  if (push.has("describe")) out.push("posts rewritten PR descriptions without asking");
  if (on(flags, "TRIAGE_VERIFY_AUTOHUNT")) out.push("picks PRs for security reviews and sandbox tests on its own");
  return out;
}

export function unattendedPushes(flags: Record<string, string>): boolean {
  const push = autopushParts(flags);
  return push.has("update") || push.has("rebase") || push.has("resolve") || push.has("fix");
}
