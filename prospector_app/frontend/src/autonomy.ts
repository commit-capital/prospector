import type { WorkerFlags } from "./api";

/** The autonomy-policy vocabulary: the lane switches Setup toggles, the
 *  Policy page discloses, and the header's mode cluster summarizes — one
 *  source, so every surface names the same behavior the same way. */

/** One lane switch, in the order a machine is provisioned. `needs` names the
 *  readiness checks that must pass first — the autofix lane is meaningless
 *  without a push identity, and an unattended agent fix without the profile's
 *  opt-in, so each control says so rather than failing later. */
/** A plain switch writes "1" when ticked. A switch with `parts` owns those
 *  names inside its key's comma-separated value, so two switches can share
 *  TRIAGE_FIX_AUTOPUSH; `id` tells them apart in the UI. */
export interface AutonomySwitch {
  key: string;
  id?: string;
  label: string;
  hint: string;
  needs?: string[];
  parts?: string[];
}

export const SWITCHES: AutonomySwitch[] = [
  { key: "TRIAGE_VERIFY_WORKER", label: "Test pull requests",
    hint: "when someone queues a pull request for testing, run its tests here in a sealed-off container to prove the fix works: they fail before the change and pass after" },
  { key: "TRIAGE_VERIFY_AUTOHUNT", label: "Look for work on its own",
    hint: "when nothing is queued, pick healthy pull requests and run security reviews and tests on them without being asked" },
  { key: "TRIAGE_FIX_WORKER", label: "Prepare fixes",
    hint: "when someone clicks a fix in the app, do the work here — bring the contributor's branch up to date, or have the AI draft a fix. Every result waits here for a person's approval before anything is pushed",
    needs: ["push_identity"] },
  { key: "TRIAGE_FIX_AUTOHUNT", label: "Queue branch updates on its own",
    hint: "spot pull requests that have fallen behind the main branch and queue the branch update without being asked",
    needs: ["push_identity"] },
  { key: "TRIAGE_FIX_HUNT_FIX", label: "Draft fixes on its own",
    hint: "spot pull requests that pass their tests but scored below the review bar, and have the AI draft a fix without being asked — or, when the reviewer's only complaint is the description, a new description that follows the template. One try per version of the pull request, and each draft waits for approval",
    needs: ["push_identity", "fix_policy"] },
  { key: "TRIAGE_FIX_HUNT_RESOLVE", label: "Resolve merge conflicts on its own",
    hint: "when a branch update it queued runs into a real conflict, have the AI resolve it instead of giving up — the result waits for approval, with its reasoning per file",
    needs: ["push_identity"] },
  { key: "TRIAGE_FIX_AUTOPUSH", parts: ["update", "rebase"], label: "Push branch updates without asking",
    hint: "a branch update or rebase that passes the build check is pushed straight to the contributor's branch instead of waiting here for approval. AI-drafted fixes always wait",
    needs: ["push_identity"] },
  { key: "TRIAGE_FIX_AUTOPUSH", id: "TRIAGE_FIX_AUTOPUSH:resolve", parts: ["resolve"],
    label: "Push agent-resolved conflicts without asking",
    hint: "an AI conflict resolution is pushed only after two independent AI reviewers both fail to find anything wrong with it, the tests related to the conflicted files pass in the sandbox, and the files are not high-risk — anything less waits here for you, with the reviewers' reasons",
    needs: ["push_identity"] },
];

export const switchId = (s: { key: string; id?: string }): string => s.id ?? s.key;

/** Stable ordering for the composed TRIAGE_FIX_AUTOPUSH value. */
export const AUTOPUSH_ORDER = ["update", "rebase", "fix", "describe", "resolve"];

export const valueParts = (value: string): Set<string> =>
  new Set(value.split(",").map((p) => p.trim()).filter(Boolean));

export const isOn = (flags: WorkerFlags, s: { key: string; parts?: string[] }): boolean =>
  s.parts
    ? s.parts.every((p) => valueParts(flags[s.key] ?? "").has(p))
    : (flags[s.key] ?? "") !== "";

/** What each unattended-push part reads as in the header's mode cluster.
 *  `update` and `rebase` share one phrase because one switch owns them both. */
const AUTOPUSH_PHRASE: Record<string, string> = {
  update: "branch updates", rebase: "branch updates",
  fix: "fixes", describe: "descriptions", resolve: "conflicts",
};

/** The unattended-push actions as a short comma-joined phrase ("branch
 *  updates, conflicts"), empty when nothing pushes without approval. */
export function autopushSummary(autopush: readonly string[]): string {
  const on = new Set(autopush);
  const phrases: string[] = [];
  for (const part of AUTOPUSH_ORDER) {
    if (!on.has(part)) continue;
    const phrase = AUTOPUSH_PHRASE[part];
    if (phrase && !phrases.includes(phrase)) phrases.push(phrase);
  }
  return phrases.join(", ");
}

/** The role each write identity plays, named once so every surface uses the
 *  same words for the same account. */
export const IDENTITY_ROLES = {
  bot: "posts, closes, and merges upstream",
  push: "pushes to contributors' PR branches",
  operator: "reads GitHub",
} as const;
