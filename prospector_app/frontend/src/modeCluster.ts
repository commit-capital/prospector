import type { FixRequestAction } from "./api";

/** What each unattended-push action reads as in the header, in display order. */
const ACTION_NOUNS: [FixRequestAction, string][] = [
  ["update", "updates"],
  ["rebase", "rebases"],
  ["resolve", "conflicts"],
  ["fix", "fixes"],
  ["describe", "descriptions"],
];

/** The header's one-line read of the unattended-push policy in force:
 *  "autonomous: updates, rebases, conflicts" names what is pushed without
 *  asking; "asks first" means every prepared change parks for approval. */
export function autonomyLabel(active: FixRequestAction[]): string {
  const names = ACTION_NOUNS.filter(([a]) => active.includes(a)).map(([, n]) => n);
  return names.length > 0 ? `autonomous: ${names.join(", ")}` : "asks first";
}

/** The identities the system acts under, as one short phrase: the posting bot,
 *  plus the contributor-push user when it is configured and distinct. */
export function identityLabel(botLogin: string, pushLogin: string | null): string {
  if (!pushLogin || pushLogin === botLogin) return `as ${botLogin}`;
  return `as ${botLogin} + ${pushLogin}`;
}
