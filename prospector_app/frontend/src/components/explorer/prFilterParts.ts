import type { FilterSpec } from "../../api";
import type { FilterPart } from "../shared/FilterSummary";
import { checkLabel, CHECK_STATUS_LABEL } from "./checkDefs";

function joinEnum(v: string | string[]): string {
  return Array.isArray(v) ? v.join(" or ") : v;
}

// The spec with just the named filter keys removed — backs a chip's × so it
// clears only its own filter, leaving the rest of the spec intact.
function omit(spec: FilterSpec, ...keys: (keyof FilterSpec)[]): FilterSpec {
  const next = { ...spec };
  for (const k of keys) delete next[k];
  return next;
}

/** Clearable clauses for the PR Explorer's active filter spec, one per active
 *  filter — feeds the shared FilterSummary chip bar. Each clause's × clears just
 *  that filter via `onChange`. */
export function buildPrFilterParts(spec: FilterSpec, onChange: (next: FilterSpec) => void,
                                   reviewerLabels: Record<string, string> = {}): FilterPart[] {
  const parts: FilterPart[] = [];
  const push = (key: string, label: string, ...keys: (keyof FilterSpec)[]) => {
    parts.push({ key, label, onClear: () => onChange(omit(spec, ...keys)) });
  };

  if (spec.q) push("q", `matches "${spec.q}"`, "q");
  if (spec.numbers !== undefined) {
    push("numbers", `PR # in (${spec.numbers.join(", ")})`, "numbers");
  }
  if (spec.risk_tier !== undefined) {
    const tiers = Array.isArray(spec.risk_tier) ? spec.risk_tier : [spec.risk_tier];
    push("risk_tier", `tier ${tiers.join(" or ")}`, "risk_tier");
  }
  if (spec.merge_ok === true) push("merge_ok", "merge gate: ready", "merge_ok");
  if (spec.merge_ok === false) push("merge_ok", "merge gate: blocked", "merge_ok");
  if (spec.has_summary === true) push("has_summary", "has agent summary", "has_summary");
  if (spec.has_summary === false) push("has_summary", "no agent summary", "has_summary");
  if (spec.has_issues === true) push("has_issues", "has linked issues", "has_issues");
  if (spec.has_issues === false) push("has_issues", "no linked issues", "has_issues");
  if (spec.safety) push("safety", `safety ${joinEnum(spec.safety)}`, "safety");
  if (spec.disposition) push("disposition", `disposition: ${joinEnum(spec.disposition)}`, "disposition");
  if (spec.drift) push("drift", `drift: ${joinEnum(spec.drift)}`, "drift");
  if (spec.ci) push("ci", `CI: ${joinEnum(spec.ci)}`, "ci");
  for (const clause of spec.checks ?? []) {
    const statuses = Array.isArray(clause.status) ? clause.status : [clause.status];
    const statusLabel = statuses.map((s) => CHECK_STATUS_LABEL[s]).join(" or ");
    parts.push({
      key: `check_${clause.key}`,
      label: `${checkLabel(clause.key)}: ${statusLabel}`,
      onClear: () => {
        const rest = (spec.checks ?? []).filter((c) => c.key !== clause.key);
        const next = { ...spec };
        if (rest.length) next.checks = rest;
        else delete next.checks;
        onChange(next);
      },
    });
  }
  if (spec.threat) push("threat", `threat: ${spec.threat}`, "threat");
  if (spec.cluster !== undefined) push("cluster", `cluster #${spec.cluster}`, "cluster");
  if (spec.cluster_none) push("cluster_none", "no cluster", "cluster_none");
  if (spec.author) push("author", `author starts with "${spec.author}"`, "author");
  if (spec.paths) push("paths", `path contains "${spec.paths}"`, "paths");
  if (spec.draft === true) push("draft", "drafts only", "draft");
  if (spec.draft === false) push("draft", "non-drafts only", "draft");
  if (spec.state === "closed") push("state", "state: closed only", "state");
  if (spec.state === "all") push("state", "state: all (open + closed)", "state");
  if (spec.trusted_author) push("trusted_author", "trusted author", "trusted_author");
  if (spec.clean) push("clean", "gate-clean", "clean");
  if (spec.conflicts === true) push("conflicts", "has conflicts", "conflicts");
  if (spec.conflicts === false) push("conflicts", "no conflicts", "conflicts");
  if (spec.has_tests === true) push("has_tests", "has tests", "has_tests");
  if (spec.has_tests === false) push("has_tests", "no tests", "has_tests");
  if (spec.responses) {
    const LABELS: Record<string, string> = {
      any: "any response", reopened: "reopened", new_commits: "new commits",
      replied: "replied", resubmitted: "resubmitted",
    };
    const label = Array.isArray(spec.responses)
      ? spec.responses.map((r) => LABELS[r] ?? r).join(" or ")
      : (LABELS[spec.responses] ?? spec.responses);
    push("responses", `response: ${label}`, "responses");
  }
  if (spec.greptile !== undefined) push("greptile", `greptile ${spec.greptile.op} ${spec.greptile.value ?? "?"}`, "greptile");
  if (spec.greptile_stale === true) push("greptile_stale", "Greptile stale", "greptile_stale");
  if (spec.greptile_stale === false) push("greptile_stale", "Greptile current", "greptile_stale");
  if (spec.greptile_severity === "defects") push("greptile_severity", "Greptile flagged a real defect", "greptile_severity");
  if (spec.greptile_severity === "nits") push("greptile_severity", "Greptile nitpicks only", "greptile_severity");
  for (const [rid, status] of Object.entries(spec.reviewer_status ?? {})) {
    const label = reviewerLabels[rid] ?? rid;
    const statuses = Array.isArray(status) ? status.join(" or ") : status;
    parts.push({
      key: `reviewer_${rid}`,
      label: `${label}: ${statuses}`,
      onClear: () => {
        const rest = { ...(spec.reviewer_status ?? {}) };
        delete rest[rid];
        const next = { ...spec };
        if (Object.keys(rest).length) next.reviewer_status = rest;
        else delete next.reviewer_status;
        onChange(next);
      },
    });
  }
  if (spec.age_days !== undefined) {
    const label = spec.age_days.op === ">" ? "older than" : "newer than";
    push("age_days", `age ${label} ${spec.age_days.value ?? "?"}d`, "age_days");
  }
  if (spec.loc) {
    const { metric, scope, op, value } = spec.loc;
    if (value !== undefined) {
      const dir = op === ">" ? "more than" : "less than";
      const what = metric === "additions" ? "added" : metric === "deletions" ? "removed" : "changed";
      push("loc", `${what} ${dir} ${value} ${scope === "effective" ? "effective" : "raw"} lines`, "loc");
    }
  }
  if (spec.files && spec.files.value !== undefined) {
    const dir = spec.files.op === ">" ? "more than" : "fewer than";
    push("files", `${dir} ${spec.files.value} files`, "files");
  }
  if (spec.pain !== undefined) {
    const label = spec.pain.op === ">" ? "above" : "below";
    push("pain", `pain ${label} ${spec.pain.value ?? "?"}`, "pain");
  }
  if (spec.author_rate !== undefined) {
    const label = spec.author_rate.op === ">" ? "above" : "below";
    const pct = spec.author_rate.value != null ? Math.round(spec.author_rate.value * 100) : null;
    push("author_rate", `author rate ${label} ${pct != null ? `${pct}%` : "?"}`, "author_rate");
  }
  if (spec.artifact_dominated) push("artifact_dominated", "mostly generated", "artifact_dominated");

  return parts;
}
