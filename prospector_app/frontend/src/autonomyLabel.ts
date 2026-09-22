import type { WorkerFlags } from "./api";

/** The header's autonomy segment: a compact label and the sentences behind
 *  it, derived from this machine's worker lane switches. */
export interface AutonomyPosture {
  /** e.g. "autonomous: rebases, conflicts" · "autonomous: drafts only" · "manual" */
  label: string;
  /** Full sentences for the hover title, one per active behavior. */
  detail: string[];
}

/** The header nouns for what TRIAGE_FIX_AUTOPUSH pushes without asking, in
 *  the order Setup composes the value. `update` and `rebase` are one Setup
 *  switch, so they read as one noun. */
const PUSHES: { parts: string[]; noun: string; detail: string }[] = [
  { parts: ["update", "rebase"], noun: "rebases",
    detail: "Pushes branch updates and rebases to contributors' branches without asking." },
  { parts: ["resolve"], noun: "conflicts",
    detail: "Pushes agent-resolved merge conflicts without asking, once two AI reviewers pass them." },
  { parts: ["fix"], noun: "fixes",
    detail: "Pushes AI-drafted fixes without asking, once the review bar clears." },
  { parts: ["describe"], noun: "descriptions",
    detail: "Posts template-following PR descriptions without asking." },
];

/** Work this machine takes on by itself; each result waits for a person. */
const HUNTS: { key: string; detail: string }[] = [
  { key: "TRIAGE_VERIFY_AUTOHUNT", detail: "Runs security reviews and tests on healthy PRs without being asked." },
  { key: "TRIAGE_FIX_AUTOHUNT", detail: "Queues branch updates on its own." },
  { key: "TRIAGE_FIX_HUNT_FIX", detail: "Drafts fixes on its own; each waits for approval." },
  { key: "TRIAGE_FIX_HUNT_RESOLVE", detail: "Resolves merge conflicts on its own; each waits for approval." },
];

/** The lanes that process work someone queued from the app. */
const WORKERS: { key: string; detail: string }[] = [
  { key: "TRIAGE_VERIFY_WORKER", detail: "Tests queued pull requests here." },
  { key: "TRIAGE_FIX_WORKER", detail: "Prepares queued fixes here; results wait for approval." },
];

const valueParts = (value: string | undefined): Set<string> =>
  new Set((value ?? "").split(",").map((p) => p.trim()).filter(Boolean));

export function autonomyPosture(flags: WorkerFlags): AutonomyPosture {
  const pushed = valueParts(flags["TRIAGE_FIX_AUTOPUSH"]);
  const nouns: string[] = [];
  const detail: string[] = [];
  for (const p of PUSHES) {
    if (p.parts.some((x) => pushed.has(x))) {
      nouns.push(p.noun);
      detail.push(p.detail);
    }
  }
  for (const h of [...HUNTS, ...WORKERS]) {
    if ((flags[h.key] ?? "") !== "") detail.push(h.detail);
  }
  if (nouns.length > 0) return { label: `autonomous: ${nouns.join(", ")}`, detail };
  if (detail.length > 0) return { label: "autonomous: drafts only", detail };
  return { label: "manual", detail: ["No automated work runs on this machine."] };
}
