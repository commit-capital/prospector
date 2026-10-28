import type { IssueDetail, IssueFixHold } from "../../api";

export type HoldKind = IssueFixHold["kind"] | "partial" | "handed-back";
export interface HoldReason { kind: HoldKind; text: string }

const HEADLINE: Record<IssueFixHold["kind"], (d: IssueDetail) => string> = {
  intake: (d) => `The intake audit read the report as ${d.fix_run?.intake?.verdict ?? "unclear"}`,
  rival: () => "Another open PR covers what ours misses",
  single: () => "Only one candidate's tests reproduce the bug",
  boundary: (d) => d.fix_run?.boundary?.crossings?.length ? "It crosses a trust boundary"
    : "The trust-boundary review gave no answer",
};

/** Why the issue's fix waits on the operator, most pressing first: a PR handed
 *  back to them, the holds on an unproposed fix (trust_boundary.holds), and a
 *  fix that covers only part of its report (fix_review.partial). */
export function holdReasons(d: IssueDetail): HoldReason[] {
  const out: HoldReason[] = [];
  const fu = d.fix_followup;
  if (fu?.state === "handed-back" && String(fu.pr) === String(d.fix_pr)) {
    out.push({ kind: "handed-back", text: `#${fu.pr} came back to you` });
  }
  for (const h of d.fix_holds ?? []) out.push({ kind: h.kind, text: HEADLINE[h.kind](d) });
  const cov = d.fix_coverage;
  if (d.fix_partial && cov) {
    const handled = cov.defects.filter((c) => c.status !== "not-addressed").length;
    out.push({ kind: "partial",
               text: `Partial fix: ${handled} of ${cov.defects.length} reported defects handled` });
  }
  return out;
}

export interface TestFailures {
  /** The failing tests, as the runner names them. */
  tests: string[];
  /** Each distinct error the run raised. */
  errors: string[];
  /** The runner's count line, e.g. "2 failed | 10 passed (12)". */
  summary: string | null;
  /** The run's last lines, for when neither could be read. */
  tail: string[];
}

// Terminal colour codes: ESC [ params letter.
const ANSI = new RegExp(`${String.fromCharCode(27)}\\[[0-9;]*[A-Za-z]`, "g");
// vitest/jest "FAIL <file> > <test>", pytest "FAILED <id> - <error>", jest "● <suite> › <test>".
const FAILED = /^\s*(?:FAIL|FAILED|●)\s+(?:@\S+\s+)?(.+?)\s*$/;
// vitest "× <test>", jest "✕ <test> (5 ms)": the reporters' tick lists, used only without the above.
const CROSSED = /^\s*[×✕✗]\s+(.+?)(?:\s+\(?\d+\s*ms\)?)?\s*$/;
// vitest "Tests  2 failed | 10 passed (12)", jest "Tests: 2 failed, 10 passed", pytest "== 2 failed in 1s ==".
const SUMMARY = /^\s*(?:Tests:?\s+|=+\s*)(\d+ failed.*?)\s*=*\s*$/;
const ERROR = /^\s*(?:E\s+)?([A-Z]\w*(?:Error|Exception)\b.*?)\s*$/;

export const plainOutput = (output: string): string => output.replace(ANSI, "");

/** What a rival PR's tests reported when run against our fix, read from the
 *  output's tail (second_opinion.py keeps the last few thousand characters). */
export function testFailures(output: string): TestFailures {
  const lines = plainOutput(output).split("\n");
  const grab = (re: RegExp) => [...new Set(lines.map((l) => re.exec(l)?.[1]).filter((m): m is string => !!m))];
  const failed = grab(FAILED);
  return {
    tests: failed.length ? failed : grab(CROSSED),
    errors: grab(ERROR),
    summary: grab(SUMMARY).at(-1) ?? null,
    tail: lines.filter((l) => l.trim()).slice(-12),
  };
}
