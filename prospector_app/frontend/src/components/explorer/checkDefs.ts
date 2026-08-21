import type { CheckClause, CheckStatus } from "../../api";

// Mirrors the stable check keys prospector_app/backend/pr_checks.py assigns to
// each row in a PR's checks rollup (`_c(key, name, ...)`) — the per-check
// filter (#578) matches on these, not the display name, since the name varies
// with the default branch. `review` aggregates every active code reviewer's
// bar and `scans` every active security scanner's.
export interface CheckDef { key: string; label: string }

export const CHECK_DEFS: CheckDef[] = [
  { key: "review", label: "Code review" },
  { key: "ci", label: "CI" },
  { key: "scans", label: "Security scans" },
  { key: "mergeable", label: "No merge conflicts" },
  { key: "tests", label: "Includes tests" },
  { key: "drift", label: "Still applies to base branch" },
  { key: "secrets", label: "No committed secrets" },
  { key: "security", label: "Deep security review" },
  { key: "verify", label: "Dynamic verification" },
];

export function checkLabel(key: string): string {
  return CHECK_DEFS.find((d) => d.key === key)?.label ?? key;
}

// Every check key the checks rollup carries, required to pass.
export const ALL_CHECKS_PASS: CheckClause[] =
  CHECK_DEFS.map((d) => ({ key: d.key, status: "pass" }));

export const CHECK_STATUS_OPTS: { v: CheckStatus; label: string; short: string }[] = [
  { v: "pass", label: "Passed", short: "✓" },
  { v: "fail", label: "Failed", short: "✗" },
  { v: "never_ran", label: "Never ran", short: "•" },
];

export const CHECK_STATUS_LABEL: Record<CheckStatus, string> = {
  pass: "passed",
  fail: "failed",
  never_ran: "never ran",
};
