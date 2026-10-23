import type { IssueDetail, IssueFixRivalEntry } from "../../api";
import { REFERENCED } from "./issueLinkChips.ts";

export type RivalRow = { pr: number; entry: IssueFixRivalEntry | null };

/** The other authors' open PRs that reference the issue: the ones an operator
 *  may go with in place of our fix (issue_triage/fix_choice.py). */
export function otherOpenPRs(d: IssueDetail): number[] {
  const ours = String(d.fix_run?.proposal?.pr ?? "");
  return d.linked_prs
    .filter((p) => REFERENCED.has(p.how ?? "") && p.state === "open" && String(p.pr) !== ours
                   && p.facts?.author_kind !== "prospector")
    .map((p) => p.pr);
}

/** Our fixed attempt against each other open PR that references the issue:
 *  the second opinion's entry for it, or null where none was run. Empty until
 *  we have a fix. */
export function rivalRows(d: IssueDetail): RivalRow[] {
  const run = d.fix_run;
  if (run?.ending !== "fixed") return [];
  const entries = new Map((run.second_opinion ?? []).map((e) => [e.pr, e]));
  return [...new Set([...entries.keys(), ...otherOpenPRs(d)])]
    .map((pr) => ({ pr, entry: entries.get(pr) ?? null }));
}
