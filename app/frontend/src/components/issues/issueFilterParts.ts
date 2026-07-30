import type { IssueFilterSpec } from "../../api";

function joinEnum(v: string | string[]): string {
  return Array.isArray(v) ? v.join(" or ") : v;
}

/** Human-readable clauses for the Issues table's active per-column filters,
 *  one per active filter — feeds the shared FilterSummary chip bar. The
 *  GitHub-state and triage-disposition segmented controls have their own
 *  persistent on/off UI, so they aren't repeated here. */
export function buildIssueFilterParts(spec: IssueFilterSpec): string[] {
  const parts: string[] = [];
  if (spec.author) parts.push(`author starts with "${spec.author}"`);
  if (spec.pain !== undefined) {
    const label = spec.pain.op === ">" ? "above" : "below";
    parts.push(`pain ${label} ${spec.pain.value ?? "?"}`);
  }
  if (spec.repro_grade) parts.push(`repro grade: ${joinEnum(spec.repro_grade)}`);
  if (spec.subsystem) parts.push(`subsystem contains "${spec.subsystem}"`);
  if (spec.dups !== undefined) {
    const label = spec.dups.op === ">" ? "more than" : "fewer than";
    parts.push(`${label} ${spec.dups.value ?? "?"} duplicates`);
  }
  if (spec.linked_prs !== undefined) {
    const label = spec.linked_prs.op === ">" ? "more than" : "fewer than";
    parts.push(`${label} ${spec.linked_prs.value ?? "?"} linked PRs`);
  }
  if (spec.labels) parts.push(`label contains "${spec.labels}"`);
  return parts;
}
