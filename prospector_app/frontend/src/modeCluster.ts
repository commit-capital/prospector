/** The header mode cluster's wording: what this deployment pushes without
 *  asking, and under which account names. One module so the cluster label and
 *  its tooltip always name the same policy the same way. */

/** Header-length name for each unattended-push action, in display order.
 *  `update` and `rebase` share one name — both read as branch updates. */
const AUTOPUSH_NAMES: [string, string][] = [
  ["update", "branch updates"],
  ["rebase", "branch updates"],
  ["fix", "fixes"],
  ["describe", "descriptions"],
  ["resolve", "conflicts"],
];

/** "branch updates, conflicts" — the friendly names for the actions
 *  TRIAGE_FIX_AUTOPUSH grants, deduplicated, in display order. */
export function autopushNames(autopush: string[]): string[] {
  const names: string[] = [];
  for (const [action, name] of AUTOPUSH_NAMES) {
    if (autopush.includes(action) && !names.includes(name)) names.push(name);
  }
  return names;
}

/** The cluster's policy segment, e.g.
 *  "autonomous: branch updates, conflicts · as my-triage-bot". When the
 *  unattended pushes run under a different account than the bot App posts as,
 *  both names appear, so the identity acting is never a guess. */
export function modePolicyLabel(autopush: string[], botLogin: string, pushLogin: string | null): string {
  const names = autopushNames(autopush);
  const policy = names.length ? `autonomous: ${names.join(", ")}` : "asks before pushing";
  const pushes = names.length && pushLogin && pushLogin !== botLogin ? ` · pushes as ${pushLogin}` : "";
  return `${policy} · as ${botLogin}${pushes}`;
}

/** The cluster's tooltip: the same policy spelled out, plus where it is set. */
export function modePolicyTitle(autopush: string[], botLogin: string, pushLogin: string | null,
                                repo: string | null): string {
  const names = autopushNames(autopush);
  const posts = `Comments, closes, reviews and merges on ${repo ?? "the triage repo"} are posted as ${botLogin}.`;
  const pushes = names.length
    ? `Once every gate passes, ${names.join(", ")} are pushed to contributors' PR branches without asking, as ${pushLogin ?? "the contributor-push account"}. Everything else waits for a person's approval.`
    : "Every prepared change waits for a person's approval before it is pushed.";
  return `${posts} ${pushes} Click for the policy (Setup).`;
}
