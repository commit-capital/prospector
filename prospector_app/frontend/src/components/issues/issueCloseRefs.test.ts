import assert from "node:assert/strict";
import { test } from "node:test";
import type { IssuePR, IssueRow } from "../../api";
import { issueCanonical, issueFixer, perIssueRefs } from "./issueCloseRefs.ts";

function row(n: number, extra: Partial<IssueRow> = {}): IssueRow {
  return {
    number: n, title: `Issue ${n}`, author: "someone", labels: [], comments: 0, reactions: 0,
    thumbs_up: 0, state: "open", url: "", subsystem: null, repro_grade: null, repro_score: null,
    pain: null, cluster: null, cluster_size: 0, canonical: null, is_dup: false, duplicates: [],
    disposition: null, fixed_by: null, linked_prs: [], linked_pr_count: 0, referenced_pr_count: 0,
    referenced_merged_count: 0,
    ...extra,
  };
}

const pr = (n: number, how: IssuePR["how"], state: IssuePR["state"]): IssuePR => ({ pr: n, how, state });

test("issueFixer takes the fix scan's fixer even when the store never saw that PR", () => {
  const r = row(1, { fixed_by: 12, linked_prs: [pr(12, "fix-found", null), pr(11, "explicit", "merged")] });
  assert.equal(issueFixer(r), 12);
});

test("issueFixer falls back to an explicit merged reference", () => {
  const r = row(1, { linked_prs: [pr(10, "explicit", "open"), pr(11, "explicit", "merged")] });
  assert.equal(issueFixer(r), 11);
});

test("issueFixer ignores unmerged, unknown-state, issue-ref, and subsystem-only PRs", () => {
  const r = row(1, { linked_prs: [pr(10, "explicit", "open"), pr(13, "explicit", null), pr(11, "issue-ref", "merged"), pr(12, "subsystem", "merged"), pr(14, "fix-found", "merged")] });
  assert.equal(issueFixer(r), null);
});

test("issueCanonical is the row's canonical unless it is the row itself", () => {
  assert.equal(issueCanonical(row(5, { canonical: 3 })), 3);
  assert.equal(issueCanonical(row(5, { canonical: 5 })), null);
  assert.equal(issueCanonical(row(5)), null);
});

test("perIssueRefs maps each selected issue to its own reference and lists the rest as missing", () => {
  const rows = [
    row(1, { linked_prs: [pr(10, "explicit", "merged")] }),
    row(2),
    row(3, { fixed_by: 30 }),
    row(4, { linked_prs: [pr(40, "explicit", "merged")] }),
  ];
  const out = perIssueRefs(rows, [1, 2, 3, 9], "fixed");
  assert.deepEqual(out.refs, { 1: 10, 3: 30 });
  assert.deepEqual(out.missing, [2, 9]);
});

test("perIssueRefs uses canonicals for a duplicate close", () => {
  const rows = [row(1, { canonical: 7 }), row(2, { canonical: 2 })];
  const out = perIssueRefs(rows, [1, 2], "dup");
  assert.deepEqual(out.refs, { 1: 7 });
  assert.deepEqual(out.missing, [2]);
});
