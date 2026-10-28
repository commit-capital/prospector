import type { IssueDetail } from "../../api";

/** The choice the Summary's suggested action puts to the operator when the
 *  issue's fix is open as a PR and waits on their review: approve it as it
 *  stands (fix_review.approve), or send it back for a full fix. `partial` is the
 *  approval's default: a fix the reviews or the issue analysis call incomplete
 *  should not close the issue when it merges. */
export interface FixDecision { pr: number; partial: boolean }

export function fixDecision(d: IssueDetail): FixDecision | null {
  const req = d.fix_request;
  if (d.state !== "open" || d.fix_pr == null || d.fix_status !== "review") return null;
  if (req && (req.status === "queued" || req.status === "running")) return null;
  return { pr: d.fix_pr, partial: !!d.fix_partial || d.analysis?.disposition === "needs-human" };
}

// The claim-test rulings that take a defect out of the report
// (issue_gates.OUT_OF_REPORT_RULINGS).
const OUT_OF_REPORT = new Set(["outside-report", "deliberate"]);
const key = (s: string) => s.toLowerCase().split(/\s+/).filter(Boolean).join(" ");

/** The send-back's starting words: the reviewer's reasons and the defects the
 *  reviews say the fix leaves, less those a claim test ruled out of the
 *  report, as the operator's comments to the fixer. */
export function sendBackDraft(d: IssueDetail): string {
  const ruled = new Map((d.fix_run?.claims ?? []).map((c) => [key(c.defect), c.ruling]));
  const left = (d.fix_run?.reviews ?? []).flatMap((r) => r.coverage ?? [])
    .filter((c) => c.status === "not-addressed" && !OUT_OF_REPORT.has(ruled.get(key(c.defect)) ?? ""))
    .map((c) => c.defect);
  return [
    `I agree with the reviewer: #${d.fix_pr} doesn't fix all of #${d.number}.`,
    d.analysis?.rationale ?? "",
    left.length ? `Still to fix:\n${[...new Set(left)].map((x) => `- ${x}`).join("\n")}` : "",
    `Revise #${d.fix_pr} so it fixes every bug #${d.number} reports, not only the first.`,
  ].filter(Boolean).join("\n\n");
}
