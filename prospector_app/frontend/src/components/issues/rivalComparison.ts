import type { IssueDetail, IssueFixRivalEntry } from "../../api";
import { REFERENCED } from "./issueLinkChips.ts";

export type RivalRow = { pr: number; entry: IssueFixRivalEntry | null };

/** Our fixed attempt against each other open PR that references the issue:
 *  the second opinion's entry for it, or null where none was run. Empty until
 *  we have a fix. */
export function rivalRows(d: IssueDetail): RivalRow[] {
  const run = d.fix_run;
  if (run?.ending !== "fixed") return [];
  const ours = String(run.proposal?.pr ?? "");
  const entries = new Map((run.second_opinion ?? []).map((e) => [e.pr, e]));
  const open = d.linked_prs
    .filter((p) => REFERENCED.has(p.how ?? "") && p.state === "open" && String(p.pr) !== ours)
    .map((p) => p.pr);
  return [...new Set([...entries.keys(), ...open])].map((pr) => ({ pr, entry: entries.get(pr) ?? null }));
}
